"""Continuous employer and career-page discovery for the job feed (v3, Nov 1 2026).

Every run:
  1. Grows boards.json, the list of verified company job boards, from four places:
     - links in the aggregator rows (Indeed and LinkedIn often link to the company's own board; short links such as
       grnh.se and tracking links such as recruitics are opened to find it),
     - companies in those rows that have no known board (their name is tried on Greenhouse, Lever and Ashby),
     - seeds.txt, NYC employers that hire for finance and planning (tried a few dozen a run),
     - once a week, Common Crawl's public index of Greenhouse and Ashby board pages (rate-limited, so slow and small).
     A board is kept only when it lists at least one job in the NYC area or remote in the US.
  2. Reads every board directly (no aggregator in between) and keeps finance and planning jobs in the NYC area or
     remote US, including ones with unconventional titles when the description is plainly planning / FP&A work.
  3. Measures itself: jobs found directly, new employers, boards that failed.
Boards that fail five runs in a row are tried weekly; boards with no NYC jobs three runs in a row are read weekly.
"""
import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone

import ats

DB_PATH = "boards.json"
SEEDS = "seeds.txt"
PROBE_BUDGET = 30          # company names tried per run
CC_VERIFY = 80             # Common Crawl candidates checked per run
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
LOCAL = ["new york", "nyc", "manhattan", "brooklyn", "jersey city", "hoboken", "stamford"]
FOREIGN = ["canada", "london", "united kingdom", "india", "emea", "europe", "apac", "singapore", "dublin", "toronto", "sydney", "paris",
           "berlin", "tokyo", "mexico", "brazil", "israel"]
CORE = re.compile(r"forecast|budget|variance analysis|financial model|fp&a|p&l|planning (and|&) analysis|headcount plan|long[- ]range plan|annual plan|scenario (planning|analysis)", re.I)
ACCT = re.compile(r"journal entr|general ledger|\bgl\b|reconcil|month[- ]end|accruals?|accounts (payable|receivable)|invoice|audit|gaap", re.I)
NEVER = re.compile(r"\b(spectrum|charter communications|cox (communications|enterprises|automotive)|time warner cable|bright house|xumo)\b", re.I)


def words(s):
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&.]+", " ", str(s or "").lower())).strip() + " "


def has(t, w):
    return (" " + w + " ") in t or (" " + w.rstrip(".") + " ") in t


def place_ok(loc, remote=False):
    l = str(loc or "").lower()
    if any(p in l for p in LOCAL):
        return True
    foreign = any(p in l for p in FOREIGN)
    us = bool(re.search(r"united states|usa|\bus\b|u\.s\.|north america", l))
    if remote or "remote" in l or "anywhere" in l:
        return not foreign or us
    return False


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
    """Worth fetching the description for."""
    return bool(title_kind(job["title"])) and place_ok(job["location"], job.get("remote")) and not NEVER.search(job.get("company", "") + " " + job.get("title", ""))


def keep(job):
    """Kept in the feed: a finance title, or an unconventional title whose description is planning work."""
    if not want(job):
        return False
    k = title_kind(job["title"])
    t = words(job["title"])
    if k != "strong" and any(has(t, w) for w in SOFT_EXCLUDE):   # sales / marketing seats with "financial" in the name
        return False
    if k in ("strong", "broad"):
        return True
    d = job.get("description") or ""
    core, acct = len(CORE.findall(d)), len(ACCT.findall(d))
    if core >= 4 and core > acct:
        job["unconventional"] = True
        return True
    return False


# ---- identity, shared with the finder: same employer + same normalized title + compatible place ----
SUFFIX = re.compile(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|companies|plc|lp|llp|na|group|holdings|usa|us|the|international|global)\b")


def emp_key(name):
    s = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower().replace("&", " and ").replace("'", ""))
    return re.sub(r"\s+", " ", SUFFIX.sub(" ", s).replace(" and ", " ")).strip()


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
    for n in names:
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


# ---- Common Crawl (weekly): the public web crawl's index of board pages. It throttles cloud runners hard, so: few
# requests, long backoff (15s, 30s, 60s), and a run never fails because of it. ----
CC_DOMAINS = {"greenhouse": ["job-boards.greenhouse.io", "boards.greenhouse.io"], "ashby": ["jobs.ashbyhq.com"]}


def cc_candidates(get_text, limit=3000):
    idx = json.loads(get_text("https://index.commoncrawl.org/collinfo.json"))[0]["id"]
    out = set()
    for kind, doms in CC_DOMAINS.items():
        for d in doms:
            q = urllib.parse.urlencode({"url": d + "/*", "output": "json", "fl": "url", "limit": limit})
            body = ""
            for wait in (0, 15, 30, 60):
                time.sleep(wait)
                try:
                    body = get_text(f"https://index.commoncrawl.org/{idx}-index?{q}")
                    break
                except Exception:
                    continue
            for line in body.splitlines():
                try:
                    u = json.loads(line)["url"]
                except Exception:
                    continue
                b = ats.board_of(u)
                if b and b.startswith(kind + ":"):
                    out.add(b)
    return sorted(out)


def cc_step(db, today, stats, get=ats.http, get_text=None, weekday=None):
    weekday = datetime.now(timezone.utc).weekday() if weekday is None else weekday
    cc = db["cc"]
    if (weekday == 6 or not cc.get("last")) and cc.get("last") != today:
        try:
            cand = cc_candidates(get_text or (lambda u: ats.http(u, want_json=False, tries=1, timeout=60)))
            cc["candidates"] = [c for c in cand if c not in db["boards"]]
            cc["cursor"] = 0
            stats["cc_candidates"] = len(cc["candidates"])
        except Exception as e:
            stats["cc_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        cc["last"] = today
    todo = cc["candidates"][cc["cursor"]:cc["cursor"] + CC_VERIFY]
    cc["cursor"] += len(todo)
    added = 0
    for b in todo:
        if b in db["boards"]:
            continue
        try:
            jobs = ats.fetch_board(b, lambda j: False, get)
        except Exception:
            continue
        if any(place_ok(j["location"], j.get("remote")) for j in jobs):
            add_board(db, b, (jobs[0].get("company") if jobs else "") or b.split(":", 1)[1], "commoncrawl", today, stats)
            added += 1
    stats["cc_checked"] = len(todo)
    stats["cc_added"] = added


# ---- reading every board ----
def due(info, board, today):
    """Weekly for boards that keep failing or show no NYC jobs; Workday (heavier) once a day; the rest every run."""
    d = date.fromisoformat(today)
    spread = sum(map(ord, board)) % 7
    if info.get("fails", 0) >= 5 or (info.get("polls", 0) >= 3 and info.get("quiet", 0) >= 3):
        return d.weekday() == spread and info.get("ok") != today
    if board.startswith("workday:"):
        return info.get("ok") != today
    return True


def sweep(db, today, stats, get=ats.http, seconds=SWEEP_SECONDS):
    t0 = time.time()
    boards = [b for b, info in db["boards"].items() if due(info, b, today)]
    rows, ok_boards, failed = [], set(), []

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
        nyc = [j for j in jobs if place_ok(j["location"], j.get("remote"))]
        info["nyc"] = len(nyc)
        info["quiet"] = 0 if nyc else info.get("quiet", 0) + 1
        hits = [j for j in nyc if keep(j)]
        if hits:
            info["hits"] = info.get("hits", 0) + len(hits)
            info["last_hit"] = today
        if jobs and jobs[0].get("company") and info.get("company") in ("", b.split(":", 1)[1]):
            info["company"] = jobs[0]["company"]
        for j in hits:
            rows.append(j)
    stats.update(boards_total=len(db["boards"]), boards_due=len(boards), boards_ok=len(ok_boards), boards_failed=len(failed),
                 failed_sample=failed[:8], sweep_seconds=round(time.time() - t0, 1))
    return rows, ok_boards


def to_row(j, company, today):
    site = j["id"].split(":")[0]
    desc = (j.get("description") or "").replace("\r", "")[:DESC_CAP].replace("\n", "\\n")
    return {"id": j["id"], "site": site, "title": j["title"], "company": company or j.get("company", ""), "location": j["location"],
            "is_remote": "True" if j.get("remote") else "False", "date_posted": j.get("posted", ""), "job_url": j["url"], "job_url_direct": j["url"],
            "min_amount": str(j.get("pay_min") or ""), "max_amount": str(j.get("pay_max") or ""), "interval": j.get("interval") or "",
            "description": desc, "first_seen": today, "last_seen": today, "also_on": ""}
