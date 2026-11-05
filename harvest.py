"""Employer-board registry harvested from Common Crawl's index files (Nov 1 2026). Replaces the v4 query-API step.

registry.tsv (one line per board, sorted, so day-to-day diffs stay small):
  board  status  checked  local  jobs  seen  first
  status: new (harvested, not yet checked) | local (has jobs in a commute region or remote US: it is in boards.json and read
          every run) | other (live, no local jobs) | empty (live, no jobs) | gone (404/403/410) | error (request failed)
  Rechecks: other and empty after 30 days, gone after 120, error after 2.
harvest_state.json: which crawl and which system prefix comes next, and the block within it (a checkpoint after every
  block, so an interrupted run loses at most one block).

Each run, within a time budget:
  1. harvest: read the next index blocks (ccindex.py) for 15 host prefixes over the newest 16 crawls; new board names are
     added as "new". Older crawls matter: Lever has blocked crawlers for a while, so most Lever boards are in older crawls.
  2. verify: check "new" boards (systems with mostly US employers first, then boards seen most often in the crawl) and
     due rechecks, a few hundred a run within each system's rate limit; boards with local jobs join boards.json.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import ats
import ccindex

PREFIXES = [("greenhouse", "io,greenhouse,job-boards)/"), ("greenhouse", "io,greenhouse,boards)/"), ("ashby", "com,ashbyhq,jobs)/"),
            ("lever", "co,lever,jobs)/"), ("lever", "co,lever,eu,jobs)/"), ("smartrecruiters", "com,smartrecruiters,jobs)/"),
            ("smartrecruiters", "com,smartrecruiters,careers)/"), ("workday", "com,myworkdayjobs,"), ("workable", "com,workable,apply)/"),
            ("rippling", "com,rippling,ats)/"), ("breezy", "hr,breezy,"), ("pinpoint", "com,pinpointhq,"), ("recruitee", "com,recruitee,"),
            ("teamtailor", "com,teamtailor,"), ("personio", "de,personio,jobs,")]
CRAWLS_BACK = 16
HARVEST_SECONDS = 100
HARVEST_BLOCKS = 200
VERIFY_SECONDS = 200
VERIFY_MAX = 800
WORKDAY_VERIFY_MAX = 25
# systems whose employers are mostly American first; European-heavy ones last
KIND_ORDER = ["greenhouse", "ashby", "lever", "workday", "rippling", "smartrecruiters", "workable", "breezy", "pinpoint", "recruitee",
              "teamtailor", "personio"]
RECHECK_DAYS = {"other": 30, "empty": 30, "gone": 120, "error": 2}
COLS = ["board", "status", "checked", "local", "jobs", "seen", "first"]


# ---------------- registry ----------------
def load(path):
    reg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip() or line.startswith("board\t"):
                    continue
                v = line.rstrip("\n").split("\t") + [""] * 7
                reg[v[0]] = {"status": v[1] or "new", "checked": v[2], "local": int(v[3] or 0), "jobs": int(v[4] or 0),
                             "seen": int(v[5] or 0), "first": v[6]}
    except FileNotFoundError:
        pass
    return reg


def save(reg, path):
    with open(path + ".new", "w", encoding="utf-8") as f:
        f.write("\t".join(COLS) + "\n")
        for b in sorted(reg):
            r = reg[b]
            f.write("\t".join([b, r["status"], r.get("checked", ""), str(r.get("local", 0)), str(r.get("jobs", 0)),
                               str(r.get("seen", 0)), r.get("first", "")]) + "\n")
    os.replace(path + ".new", path)


def norm_key(board):
    """Case-insensitive identity: 'ashby:OpenAI' and 'ashby:openai' are one board (the first spelling seen is kept)."""
    return board.lower()


def add(reg, index, board, today, n=1):
    k = norm_key(board)
    if k in index:
        reg[index[k]]["seen"] = reg[index[k]].get("seen", 0) + n
        return False
    reg[board] = {"status": "new", "checked": "", "local": 0, "jobs": 0, "seen": n, "first": today}
    index[k] = board
    return True


def migrate(reg, db, today):
    """Boards already in boards.json are 'local'; the v4 Common Crawl queue and rejects carry over."""
    index = {norm_key(b): b for b in reg}
    for b in db.get("boards", {}):
        if norm_key(b) not in index:
            reg[b] = {"status": "local", "checked": db["boards"][b].get("added", today), "local": db["boards"][b].get("nyc", 0),
                      "jobs": db["boards"][b].get("jobs", 0), "seen": 0, "first": db["boards"][b].get("added", today)}
            index[norm_key(b)] = b
    cc = db.pop("cc", None) or {}
    for b, n in cc.get("queue", []):
        add(reg, index, b, today, n or 1)
    for b in cc.get("candidates", []) or []:
        add(reg, index, b, today)
    for b, d in (cc.get("rejected") or {}).items():
        if norm_key(b) not in index:
            reg[b] = {"status": "other", "checked": d, "local": 0, "jobs": 0, "seen": 1, "first": d}
            index[norm_key(b)] = b
    return index


# ---------------- harvesting ----------------
def load_state(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"crawls": [], "done": {}, "cursor": None, "crawls_checked": ""}


def save_state(st, path):
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(st, f, indent=0, sort_keys=True)
    os.replace(path + ".new", path)


def _next_item(st):
    """(crawl, prefix index) not yet done: newest crawl first, then each system."""
    for c in st["crawls"][:CRAWLS_BACK]:
        done = set(st["done"].get(c, []))
        for i in range(len(PREFIXES)):
            if i not in done:
                return c, i
    return None


def harvest(reg, index, st, today, stats, get=None, crawl_list=None, seconds=None, max_blocks=None):
    get = get or ccindex.get_range
    seconds = HARVEST_SECONDS if seconds is None else seconds
    max_blocks = HARVEST_BLOCKS if max_blocks is None else max_blocks
    t0 = time.time()
    if st.get("crawls_checked") != today or not st["crawls"]:
        try:
            st["crawls"] = (crawl_list or ccindex.crawls)()[:CRAWLS_BACK]
            st["crawls_checked"] = today
        except Exception as e:
            stats["harvest_error"] = f"crawl list: {type(e).__name__}: {str(e)[:100]}"
            if not st["crawls"]:
                return
    st["done"] = {c: v for c, v in st["done"].items() if c in st["crawls"]}
    blocks_read = new = 0
    sizes = {}
    while time.time() - t0 < seconds and blocks_read < max_blocks:
        item = _next_item(st)
        if not item:
            break
        crawl, pi = item
        kind, prefix = PREFIXES[pi]
        cur = st.get("cursor") or {}
        start = cur.get("block", 0) if cur.get("crawl") == crawl and cur.get("prefix") == pi else 0
        try:
            if crawl not in sizes:
                sizes[crawl] = (getattr(get, "size", None) or ccindex.size_of)(ccindex.BASE.format(crawl=crawl) + "cluster.idx")
            lines, _ = ccindex.find_blocks(crawl, prefix, get=get, size=sizes[crawl])
        except Exception as e:
            stats["harvest_error"] = f"{crawl} {prefix}: {type(e).__name__}: {str(e)[:100]}"
            if isinstance(e, ats.HttpError) and e.code == 404:       # an old crawl without this index layout
                st["done"].setdefault(crawl, []).extend(range(len(PREFIXES)))
                continue
            break
        i = start
        while i < len(lines) and time.time() - t0 < seconds and blocks_read < max_blocks:
            try:
                recs = ccindex.read_block(crawl, lines[i], get=get)
            except Exception as e:
                stats["harvest_error"] = f"block {lines[i]['file']}: {type(e).__name__}: {str(e)[:100]}"
                break
            blocks_read += 1
            counts = {}
            for _, url in recs:
                b = ats.board_of(url)
                if b and b.startswith(kind + ":"):
                    counts[b] = counts.get(b, 0) + 1
            for b, n in counts.items():
                new += add(reg, index, b, today, n)
            i += 1
            st["cursor"] = {"crawl": crawl, "prefix": pi, "block": i}
        if i >= len(lines):
            st["done"].setdefault(crawl, []).append(pi)
            st["cursor"] = None
        else:
            break
    stats.update(harvest_blocks=blocks_read, harvest_new=new, harvest_seconds=round(time.time() - t0, 1),
                 harvest_progress=f"{sum(len(v) for v in st['done'].values())}/{len(PREFIXES) * len(st['crawls'][:CRAWLS_BACK])} crawl-system pairs")


# ---------------- verifying ----------------
def due_for_check(r, today):
    if r["status"] == "new":
        return True
    days = RECHECK_DAYS.get(r["status"])
    if days is None or not r.get("checked"):
        return False
    return (date.fromisoformat(today) - date.fromisoformat(r["checked"])).days >= days


def verify(reg, db, today, stats, place_ok, title_kind, add_board, get=ats.http, seconds=None, limit=None):
    t0 = time.time()
    seconds = VERIFY_SECONDS if seconds is None else seconds
    limit = VERIFY_MAX if limit is None else limit
    todo = [b for b, r in reg.items() if due_for_check(r, today) and b.split(":", 1)[0] in ats.FETCH]
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    todo.sort(key=lambda b: (reg[b]["status"] != "new", rank.get(b.split(":", 1)[0], 99), -reg[b].get("seen", 0)))
    wd = [b for b in todo if b.startswith("workday:")][:WORKDAY_VERIFY_MAX]
    todo = [b for b in todo if not b.startswith("workday:")][:max(0, limit - len(wd))] + wd

    def one(b):
        if time.time() - t0 > seconds:
            return b, None, "time"
        try:
            return b, ats.peek(b, get), ""
        except ats.HttpError as e:
            return b, None, "gone" if e.code in (404, 410, 403, 400) else "error"
        except Exception:
            return b, None, "error"

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, todo))
    counts = {}
    for b, jobs, err in results:
        if err == "time":
            continue
        r = reg[b]
        r["checked"] = today
        if jobs is None:
            r["status"] = err
        else:
            local = [j for j in jobs if place_ok(j.get("location"), j.get("remote"))]
            r.update(jobs=len(jobs), local=len(local), status="local" if local else ("other" if jobs else "empty"))
            if local and b not in db["boards"]:
                add_board(db, b, (jobs[0].get("company") if jobs else "") or b.split(":", 1)[1], "registry", today, stats)
                db["boards"][b].update(nyc=len(local), fin_at_add=sum(1 for j in local if title_kind(j.get("title"))))
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    stats.update(verified=sum(counts.values()), verified_by_status=counts, verify_seconds=round(time.time() - t0, 1),
                 registry_size=len(reg), registry_waiting=sum(1 for r in reg.values() if r["status"] == "new"))


def step(db, here, today, stats, place_ok, title_kind, add_board, get=ats.http, range_get=None, crawl_list=None):
    """One run of harvesting and verifying; never raises. Writes registry.tsv and harvest_state.json next to boards.json."""
    rpath, spath = os.path.join(here, "registry.tsv"), os.path.join(here, "harvest_state.json")
    reg = load(rpath)
    index = migrate(reg, db, today)
    st = load_state(spath)
    try:
        harvest(reg, index, st, today, stats, range_get, crawl_list)
    except Exception as e:
        stats["harvest_error"] = f"{type(e).__name__}: {str(e)[:150]}"
    save_state(st, spath)
    try:
        verify(reg, db, today, stats, place_ok, title_kind, add_board, get)
    except Exception as e:
        stats["verify_error"] = f"{type(e).__name__}: {str(e)[:150]}"
    save(reg, rpath)
