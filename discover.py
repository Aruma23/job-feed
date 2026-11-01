"""Continuous employer and career-page discovery for the job feed (v4, Nov 1 2026; v3 earlier the same day).

Every run:
  1. Grows boards.json, the list of verified company job boards, from four places:
     - links in the aggregator rows (Indeed and LinkedIn often link to the company's own board; short links such as
       grnh.se and tracking links such as recruitics are opened to find it),
     - companies in those rows that have no known board, most relevant first (tried on Greenhouse, Lever, Ashby and
       SmartRecruiters),
     - seeds.txt, NYC employers that hire for finance and planning (tried a few dozen a run),
     - Common Crawl's public index of board pages on all five systems (Greenhouse, Ashby, Lever, SmartRecruiters, and Workday
       through a domain query), one page of one system at a time, rotating, biggest boards checked first.
     A board is kept only when it lists at least one job in a commute region (geo.py) or remote in the US.
  2. Reads every board directly and keeps finance and planning jobs in the commute regions or remote US. Boards that
     produced matches lately are read first and every run; boards that list local jobs but never a match are read once a
     day; boards with no local jobs or that keep failing, once a week.
  3. Unconventional titles: kept when the description is plainly planning work (the old keyword rule); ones with some
     planning signal go to review (relevance.py) instead of being dropped.
"""
import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import ats
import geo
import identity
import relevance

DB_PATH = "boards.json"
SEEDS = "seeds.txt"
PROBE_BUDGET = 30          # company names tried per run
CC_VERIFY = 80             # Common Crawl candidates checked per run (Workday ones count three times: heavier)
SWEEP_SECONDS = 12 * 60    # time for reading boards
DESC_CAP = 6000

# ---- the same rules the tracker's finder uses (engine CFG), so the feed carries what the finder would keep ----
STRONG = ["fp&a", "fp & a", "fpa", "financial planning", "planning & analysis", "planning and analysis", "workforce planning", "headcount",
          "capacity planning", "strategic finance", "business finance", "finance business partner", "financial analyst", "finance analyst",
          "business planning", "revenue planning", "corporate finance", "finance associate", "strategy & finance", "strategy and finance",
          "planning analyst", "finance & strategy", "finance and strategy"]
BROAD = ["investment analyst", "portfolio analyst", "asset management", "real estate analyst", "economic", "economics", "economist",
         "research analyst", "research associate", "pricing analyst", "strategy analyst", "business operations", "bizops",
         "operations finance", "treasury analyst", "business analyst", "workforce", "staffing analyst", "finance", "financial"]
HARD_EXCLUDE = ["intern", "internship", "summer", "class of", "co-op", "coop", "accountant", "tax", "audit", "payroll", "recruiter",
                "recruiting", "sourcer", "attorney", "counsel", "engineer", "engineering", "director", "vice president", "vp", "svp", "evp",
                "head of", "chief", "principal", "controller", "billing", "accounts payable", "accounts receivable", "collections",
                "compliance", "product manager", "program manager", "data scientist", "solution architect", "solutions architect",
                "partner development", "people partner"]
SOFT_EXCLUDE = ["sales", "marketing", "account executive", "account manager", "business development"]
AMBIG = re.compile(r"\b(strategy|strategic|operations|ops|planning|insights|analytics|analyst|associate|performance|decision support|commercial|"
                   r"revenue|business|capital|budget|reporting|portfolio|treasury|investor relations|corporate development|pricing)\b", re.I)
CORE = re.compile(r"forecast|budget|variance analysis|financial model|fp&a|p&l|planning (and|&) analysis|headcount plan|long[- ]range plan|annual plan|scenario (planning|analysis)", re.I)
ACCT = re.compile(r"journal entr|general ledger|\bgl\b|reconcil|month[- ]end|accruals?|accounts (payable|receivable)|invoice|audit|gaap", re.I)
NEVER = re.compile(r"\b(spectrum|charter communications|cox (communications|enterprises|automotive)|time warner cable|bright house|xumo)\b", re.I)


def words(s):
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&.]+", " ", str(s or "").lower())).strip() + " "


def has(t, w):
    return (" " + w + " ") in t or (" " + w.rstrip(".") + " ") in t


def place_ok(loc, remote=False, text=""):
    """In a commute region (geo.REGIONS, tiers in geo.ENABLED_TIERS) or remote within the US."""
    return geo.place_ok(loc, remote, text)


def title_kind(title):
    """'strong', 'broad', 'ambiguous' or '' (excluded)."""
    t = words(title)
    if not t.strip():
        return ""
    if any(has(t, w) for w in HARD_EXCLUDE) and not has(t, "analytics engineer"):
        return ""
    if any(has(t, w) for w in STRONG):
        return "strong"
    if any(has(t, w) for w in BROAD):
        return "broad"
    return "ambiguous" if AMBIG.search(title or "") else ""


def want(job):
    """Worth fetching the description for. 'Maybe' places ("2 Locations", "Multiple locations") qualify: the detail page or
    the description says where."""
    if not title_kind(job["title"]) or NEVER.search(job.get("company", "") + " " + job.get("title", "")):
        return False
    return geo.resolve(job["location"], job.get("remote")).status in ("local", "remote_us", "maybe")


def judge(job):
    """'keep', 'borderline' (unconventional title with some planning signal: review decides) or 'drop'."""
    if not want(job):
        return "drop"
    if not place_ok(job["location"], job.get("remote"), job.get("description") or ""):
        return "drop"
    k = title_kind(job["title"])
    t = words(job["title"])
    if k != "strong" and any(has(t, w) for w in SOFT_EXCLUDE):   # sales / marketing seats with "financial" in the name
        return "drop"
    if k in ("strong", "broad"):
        return "keep"
    d = job.get("description") or ""
    core, acct = len(CORE.findall(d)), len(ACCT.findall(d))
    if core >= 4 and core > acct:
        job["unconventional"] = True
        return "keep"
    if relevance.rules(job["title"], d, k)["core"] >= 1:
        job["borderline"] = True
        return "borderline"
    return "drop"


def keep(job):
    """Kept in the feed now: a finance title, or an unconventional title whose description is plainly planning work."""
    return judge(job) == "keep"


# ---- identity, shared with the finder ----
SUFFIX = identity.SUFFIX
emp_key = identity.emp_key


def title_key(t):
    s = " " + re.sub(r"\([^)]*\)", " ", str(t or "").lower()) + " "
    s = re.sub(r"\s[-–—|,:]\s.*$", " ", s).replace("&", " and ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bfp and a\b|\bfpa\b|financial planning and analysis", " fpa ", s)
    s = re.sub(r"\bsr\b\.?", " senior ", s)
    s = re.sub(r"\b(hybrid|remote|onsite|nyc|new york|ny)\b", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


# ---- the board list ----
def load_db(path=DB_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            db = json.load(f)
    except (FileNotFoundError, ValueError):
        db = {}
    db.setdefault("version", 3)
    db.setdefault("boards", {})
    db.setdefault("neg", {})
    db.setdefault("seed_cursor", 0)
    db.setdefault("cc", {"last": "", "candidates": [], "cursor": 0})
    return db


def save_db(db, path=DB_PATH):
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=0, sort_keys=True)
    os.replace(tmp, path)


def add_board(db, board, company, src, today, stats):
    if not board or board in db["boards"]:
        return False
    db["boards"][board] = {"company": company or board.split(":", 1)[1], "src": src, "added": today, "ok": "", "fails": 0, "jobs": 0, "nyc": 0,
                           "hits": 0, "last_hit": "", "polls": 0, "quiet": 0}
    stats["boards_new"] += 1
    stats.setdefault("new_from", {}).setdefault(src, 0)
    stats["new_from"][src] += 1
    return True


def from_rows(db, rows, today, stats, resolve=ats.resolve_redirect, budget=40):
    """Boards linked from aggregator rows (after opening short and tracking links). Also returns a better link per row."""
    better = {}
    opened = 0
    for r in rows:
        u = r.get("job_url_direct") or ""
        if not u:
            continue
        real = u
        if ats.REDIRECTS.match(u) or "rx_url=" in u:
            if opened >= budget:
                continue
            opened += 1
            real = ats.unwrap(u, resolve)
        b = ats.board_of(real)
        if b:
            if real != u:
                better[r.get("id")] = real
            add_board(db, b, r.get("company"), "feed-link", today, stats)
    stats["links_opened"] = opened
    return better


def probe_names(db, names, today, stats, get=ats.http, budget=PROBE_BUDGET):
    known = {emp_key(v.get("company")) for v in db["boards"].values()}
    tried = 0
    rank = {}
    for n in names:                                   # companies named most often (by relevant rows) first
        rank[n] = rank.get(n, 0) + 1
    for n in sorted(dict.fromkeys(names), key=lambda x: -rank[x]):
        k = emp_key(n)
        if not k or k in known or NEVER.search(n):
            continue
        last = db["neg"].get(k)
        if last and (date.fromisoformat(today) - date.fromisoformat(last)).days < 14:
            continue
        if tried >= budget:
            break
        tried += 1
        b = ats.probe(n, get)
        if b:
            add_board(db, b, n, "name-probe", today, stats)
            known.add(k)
        else:
            db["neg"][k] = today
    stats["names_tried"] = stats.get("names_tried", 0) + tried
    return tried


def seeds(path=SEEDS):
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        return []


# ---- Common Crawl: the public web crawl's index of board pages. It throttles cloud runners hard, so: one system's page per
# refill (two requests), a refill only when the queue runs low (about every three days), long backoff, never fails a run. ----
CC_DOMAINS = [("greenhouse", "job-boards.greenhouse.io", "prefix"), ("ashby", "jobs.ashbyhq.com", "prefix"),
              ("lever", "jobs.lever.co", "prefix"), ("workday", "myworkdayjobs.com", "domain"),
              ("smartrecruiters", "jobs.smartrecruiters.com", "prefix"), ("greenhouse", "boards.greenhouse.io", "prefix"),
              ("lever", "jobs.eu.lever.co", "prefix"), ("smartrecruiters", "careers.smartrecruiters.com", "prefix")]
CC_LOW = 160               # refill the queue when fewer candidates than this are waiting
CC_WORKDAY_PER_RUN = 10
CC_REJECT_DAYS = 90        # a board checked with no local jobs is not checked again for this long


def _cc_get(get_text, url):
    for wait in (0, 20, 40):
        time.sleep(wait)
        try:
            return get_text(url)
        except Exception:
            continue
    return ""


def cc_page(get_text, idx, kind, dom, match, page):
    """One page of the crawl index for one system -> ({board: pages seen}, number of pages)."""
    q = {"url": dom + ("/*" if match == "prefix" else ""), "output": "json"}
    if match == "domain":
        q["matchType"] = "domain"
    meta = _cc_get(get_text, f"https://index.commoncrawl.org/{idx}-index?" + urllib.parse.urlencode(dict(q, showNumPages="true")))
    try:
        pages = max(1, int(json.loads(meta.splitlines()[0])["pages"]))
    except Exception:
        pages = 1
    body = _cc_get(get_text, f"https://index.commoncrawl.org/{idx}-index?" + urllib.parse.urlencode(dict(q, fl="url", page=page % pages)))
    seen = {}
    for line in body.splitlines():
        try:
            u = json.loads(line)["url"]
        except Exception:
            continue
        b = ats.board_of(u)
        if b and b.startswith(kind + ":"):
            seen[b] = seen.get(b, 0) + 1
    return seen, pages


def cc_candidates(get_text, limit=3000):
    """Kept for callers of v3: every system's first page."""
    idx = json.loads(get_text("https://index.commoncrawl.org/collinfo.json"))[0]["id"]
    out = set()
    for kind, dom, match in CC_DOMAINS:
        out |= set(cc_page(get_text, idx, kind, dom, match, 0)[0])
    return sorted(out)


def _cc_state(db):
    cc = db.setdefault("cc", {})
    if "queue" not in cc:                                  # v3 layout: candidates + cursor
        old = cc.get("candidates") or []
        cc["queue"] = [[b, 1] for b in old[cc.get("cursor", 0):]]
    for k, v in (("pages", {}), ("rejected", {}), ("dom_cursor", 0), ("crawl", ""), ("last", "")):
        cc.setdefault(k, v)
    cc.pop("candidates", None)
    cc.pop("cursor", None)
    return cc


def cc_refill(db, today, stats, get_text):
    cc = _cc_state(db)
    idx = json.loads(get_text("https://index.commoncrawl.org/collinfo.json"))[0]["id"]
    if idx != cc["crawl"]:
        cc.update(crawl=idx, pages={})
    kind, dom, match = CC_DOMAINS[cc["dom_cursor"] % len(CC_DOMAINS)]
    cc["dom_cursor"] += 1
    page = cc["pages"].get(dom, 0)
    seen, pages = cc_page(get_text, idx, kind, dom, match, page)
    cc["pages"][dom] = (page + 1) % pages
    queued = {b for b, _ in cc["queue"]}
    fresh = [[b, n] for b, n in seen.items() if b not in db["boards"] and b not in queued and b not in cc["rejected"]]
    cc["queue"] = sorted(cc["queue"] + fresh, key=lambda x: -x[1])[:4000]   # boards with more crawled pages (bigger hirers) first
    cc["last"] = today
    stats.update(cc_source=f"{kind} {dom} page {page + 1}/{pages}", cc_candidates=len(fresh))


def cc_step(db, today, stats, get=ats.http, get_text=None, weekday=None):
    cc = _cc_state(db)
    get_text = get_text or (lambda u: ats.http(u, want_json=False, tries=1, timeout=60))
    if len(cc["queue"]) < CC_LOW and cc.get("last") != today:
        try:
            cc_refill(db, today, stats, get_text)
        except Exception as e:
            stats["cc_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            cc["last"] = today
    checked = added = wd = 0
    budget = CC_VERIFY
    while cc["queue"] and budget > 0:
        b, _ = cc["queue"].pop(0)
        if b in db["boards"]:
            continue
        if b.startswith("workday:"):
            if wd >= CC_WORKDAY_PER_RUN:
                cc["queue"].append([b, 0])
                if all(x[0].startswith("workday:") for x in cc["queue"]):
                    break
                continue
            wd += 1
            budget -= 3
        else:
            budget -= 1
        checked += 1
        try:
            jobs = ats.fetch_board(b, lambda j: False, get)
        except Exception:
            cc["rejected"][b] = today
            continue
        local = [j for j in jobs if place_ok(j["location"], j.get("remote"))]
        if local:
            fin = sum(1 for j in local if title_kind(j["title"]))
            add_board(db, b, (jobs[0].get("company") if jobs else "") or b.split(":", 1)[1], "commoncrawl", today, stats)
            db["boards"][b].update(nyc=len(local), fin_at_add=fin)
            added += 1
        else:
            cc["rejected"][b] = today
    d0 = date.fromisoformat(today)
    cc["rejected"] = {b: d for b, d in cc["rejected"].items() if (d0 - date.fromisoformat(d)).days < CC_REJECT_DAYS}
    stats["cc_checked"] = checked
    stats["cc_added"] = added
    stats["cc_queue"] = len(cc["queue"])


# ---- reading every board ----
def due(info, board, today):
    """Every run for boards with a match in the last 14 days; once a day for Workday (heavier) and for boards that list local
    jobs but never a match after 12 reads; weekly for boards that keep failing or show no local jobs."""
    d = date.fromisoformat(today)
    spread = sum(map(ord, board)) % 7
    if info.get("fails", 0) >= 5 or (info.get("polls", 0) >= 3 and info.get("quiet", 0) >= 3):
        return d.weekday() == spread and info.get("ok") != today
    hot = bool(info.get("last_hit")) and (d - date.fromisoformat(info["last_hit"])).days <= 14
    if board.startswith("workday:"):
        return info.get("ok") != today
    if not hot and info.get("polls", 0) >= 12 and not info.get("hits"):
        return info.get("ok") != today
    return True


def priority(info, today):
    """Read order: recent matches, then many matches, then many local jobs (an out-of-time sweep skips the tail)."""
    d = date.fromisoformat(today)
    recent = (d - date.fromisoformat(info["last_hit"])).days if info.get("last_hit") else 999
    return (recent, -info.get("hits", 0), -info.get("nyc", 0))


def sweep(db, today, stats, get=ats.http, seconds=SWEEP_SECONDS):
    t0 = time.time()
    boards = sorted((b for b, info in db["boards"].items() if due(info, b, today)), key=lambda b: priority(db["boards"][b], today))
    rows, borderline, ok_boards, failed = [], [], set(), []

    def one(b):
        if time.time() - t0 > seconds:
            return b, None, "out of time"
        try:
            return b, ats.fetch_board(b, want, get), ""
        except Exception as e:
            return b, None, f"{type(e).__name__}: {str(e)[:100]}"

    fast = [b for b in boards if not b.startswith("workday:")]
    slow = [b for b in boards if b.startswith("workday:")]
    results = []
    for group, workers in ((fast, 8), (slow, 3)):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for f in as_completed([ex.submit(one, b) for b in group]):
                results.append(f.result())
    for b, jobs, err in results:
        info = db["boards"][b]
        if jobs is None:
            if err != "out of time":
                info["fails"] = info.get("fails", 0) + 1
                failed.append(b)
            continue
        info.update(ok=today, fails=0, jobs=len(jobs), polls=info.get("polls", 0) + 1)
        ok_boards.add(b)
        places = [geo.resolve(j["location"], j.get("remote")).status for j in jobs]
        info["nyc"] = sum(1 for p in places if p in ("local", "remote_us"))
        info["quiet"] = 0 if any(p in ("local", "remote_us", "maybe") for p in places) else info.get("quiet", 0) + 1
        verdicts = [(j, judge(j)) for j in jobs]
        hits = [j for j, v in verdicts if v == "keep"]
        borderline += [j for j, v in verdicts if v == "borderline"]
        if hits:
            info["hits"] = info.get("hits", 0) + len(hits)
            info["last_hit"] = today
        if jobs and jobs[0].get("company") and info.get("company") in ("", b.split(":", 1)[1]):
            info["company"] = jobs[0]["company"]
        for j in hits:
            rows.append(j)
    stats.update(boards_total=len(db["boards"]), boards_due=len(boards), boards_ok=len(ok_boards), boards_failed=len(failed),
                 failed_sample=failed[:8], sweep_seconds=round(time.time() - t0, 1), borderline=len(borderline),
                 boards_hot=sum(1 for i in db["boards"].values() if i.get("last_hit") and (date.fromisoformat(today) - date.fromisoformat(i["last_hit"])).days <= 14))
    return rows, ok_boards, borderline


def to_row(j, company, today):
    site = j["id"].split(":")[0]
    desc = (j.get("description") or "").replace("\r", "")[:DESC_CAP].replace("\n", "\\n")
    p = geo.resolve(j["location"], j.get("remote"), j.get("description") or "")
    return {"id": j["id"], "site": site, "title": j["title"], "company": company or j.get("company", ""), "location": j["location"],
            "is_remote": "True" if j.get("remote") else "False", "date_posted": j.get("posted", ""), "job_url": j["url"], "job_url_direct": j["url"],
            "min_amount": str(j.get("pay_min") or ""), "max_amount": str(j.get("pay_max") or ""), "interval": j.get("interval") or "",
            "description": desc, "first_seen": today, "last_seen": today, "also_on": "", "region": geo.label(p), "source_ids": j["id"]}
