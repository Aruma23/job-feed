"""Continuous employer and career-page discovery for the job feed (v4, Nov 1 2026; v3 earlier the same day).

Every run:
  1. Grows boards.json, the list of verified company job boards, from four places:
     - links in the aggregator rows (Indeed and LinkedIn often link to the company's own board; short links such as
       grnh.se and tracking links such as recruitics are opened to find it),
     - companies in those rows that have no known board, most relevant first (tried on Greenhouse, Lever, Ashby and
       SmartRecruiters),
     - seeds.txt, NYC employers that hire for finance and planning (tried a few dozen a run),
     - the board registry (harvest.py): every board name in Common Crawl's index files for twelve application systems over
       the newest 16 crawls, checked a few hundred a run.
     A board is kept only when it lists at least one job in a commute region (geo.py) or remote in the US.
  2. Reads every board directly and keeps finance and planning jobs in the commute regions or remote US. Boards that
     produced matches lately are read first and every run; boards that list local jobs but never a match are read once a
     day; boards with no local jobs or that keep failing, once a week.
  3. Unconventional titles: kept when the description is plainly planning work (the old keyword rule); ones with some
     planning signal go to review (relevance.py) instead of being dropped.
"""
import hashlib
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
class _Excluded:
    """Employers the feed never lists, ranks or probes (the owner's own employer group). Kept as salted hashes of 1-3 word
    phrases so this public repository names no one; the tracker and the Mac runner keep their own full lists. An optional
    repository secret FEED_EXCLUDE (a regex) adds more without a commit. search(text) is truthy on a match, like re.search."""
    SALT = b"jf-x1:"
    HASHES = frozenset({
    "1b945ae8f437a6fc4cad", "1c5d28985165d4ebfc95", "1edabff7040c60e225f7", "49d526c0b03256ecd88c",
    "542bd6c99af68db50290", "629221a84b6b0ccfdc19", "83dd4ae732b1c0963ae4", "96d751153d23fd4ac031",
    "98f7ce24e46c2b2f1111", "9935b0432004eb4003c6", "9b455f6b4e268d81ebcd", "a12db573e5eaf6693edd",
    "aa6d4b8c866eb563a892", "bb27a399db7c78ddd879", "c9928608e205dafc283a", "cef3acb3941ad215790c",
    "d9e568a9ca3b7b9f444f", "ef375990609f07012840", "f06c03ae73c52618ab69"})

    def __init__(self, phrases=None, extra=None):
        self.h = self.HASHES if phrases is None else frozenset(self.hash(p) for p in phrases)
        x = os.environ.get("FEED_EXCLUDE", "").strip() if extra is None else extra
        self.extra = re.compile(x, re.I) if x else None

    @classmethod
    def hash(cls, phrase):
        return hashlib.sha256(cls.SALT + " ".join(re.findall(r"[a-z0-9]+", phrase.lower())).encode()).hexdigest()[:20]

    def search(self, text):
        w = re.findall(r"[a-z0-9]+", str(text or "").lower())
        for n in (1, 2, 3):
            for i in range(len(w) - n + 1):
                if self.hash(" ".join(w[i:i + n])) in self.h:
                    return True
        return bool(self.extra and self.extra.search(str(text or "")))


NEVER = _Excluded()


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
        job["_drop"] = "sales or marketing title"
        return "drop"
    if k in ("strong", "broad"):
        return "keep"
    d = job.get("description") or ""
    core, acct = len(CORE.findall(d)), len(ACCT.findall(d))
    if core >= 4 and core > acct:
        job["unconventional"] = True
        return "keep"
    if relevance.rules(job["title"], d, k)["core"] >= 2:
        job["borderline"] = True
        return "borderline"
    job["_drop"] = "few planning duties"      # turned away on its description: eligible for the false-negative sample
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
    seen = {}
    for b in sorted(db["boards"], key=lambda b: (-db["boards"][b].get("hits", 0), b)):   # one board, two spellings: keep one
        if b.lower() in seen:
            del db["boards"][b]
        else:
            seen[b.lower()] = b
    return db


def save_db(db, path=DB_PATH):
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=0, sort_keys=True)
    os.replace(tmp, path)


def add_board(db, board, company, src, today, stats):
    if not board or board in db["boards"] or board.lower() in {b.lower() for b in db["boards"]}:
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


DROP_SHARE = 0.4      # a complete read below 40% of the board's usual count (usual >= 10), or empty when the board had any
DROP_ACCEPT = 3       # jobs lately, is suspect, not a mass closing ... until it repeats on 3 reads in a row: then it really shrank
PRESENT = {}          # board -> requisition keys a trusted complete read exposed this run (freshness.route_gone)
SKIPPED = set()       # boards due by schedule but skipped this run as unchanged (footprint.py); their jobs are kept
REJECTED = []         # (board, job) the rules turned away on their description this run (backlog.py samples them)


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def guard(info, jobs):
    """(jobs without repeats, complete, note). A read far below the board's usual size keeps the last good snapshot: it is
    treated as partial (nothing closes; missing jobs are checked one by one or kept a few days) until it repeats."""
    seen, uniq = set(), Jobs_like(jobs)
    for j in jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            uniq.append(j)
    note = "repeated pages" if len(uniq) < len(jobs) else ""
    complete = getattr(jobs, "complete", True)
    hist = info.get("hist", [])
    base = _median(hist) if len(hist) >= 3 else 0
    # A big board far below its usual count, or any board that listed jobs lately now answering with none at all: a changed
    # page format or an empty response reads as "complete", and one such read would close every job on it with nothing to
    # restore (a closed job is explained to the audit). Both wait DROP_ACCEPT reads before they count.
    emptied = complete and not uniq and max(hist, default=0) > 0
    if (complete and base >= 10 and len(uniq) < DROP_SHARE * base) or emptied:
        info["drop"] = info.get("drop", 0) + 1
        if info["drop"] < DROP_ACCEPT:
            return uniq, False, f"sudden drop: {len(uniq)} jobs vs usual {base or max(hist)}"
        info["hist"] = [len(uniq)]
        info["drop"] = 0
        return uniq, True, f"shrink accepted after {DROP_ACCEPT} reads: {len(uniq)} jobs (was {base})"
    info["drop"] = 0
    if complete:
        info["hist"] = (hist + [len(uniq)])[-6:]
    return uniq, complete, note


def Jobs_like(jobs):
    out = ats.Jobs()
    out.complete = getattr(jobs, "complete", True)
    return out


def adapter_health(db, today, per):
    """Per application system: boards read, failures, suspect drops, repeated pages, kept jobs missing a description,
    location or link. A system is degraded when 2+ boards (and 30%+ of those read) drop at once, half of its due boards
    fail (4+), or half of its kept jobs (5+) lack a description or location. db["adapters"] remembers since when."""
    mem = db.setdefault("adapters", {})
    out, bad = {}, []
    for prov, h in sorted(per.items()):
        why = []
        if h["drops"] >= 2 and h["drops"] >= 0.3 * max(1, h["read"]):
            why.append(f"{h['drops']} of {h['read']} boards suddenly shrank")
        if h["due"] >= 4 and h["failed"] >= 0.5 * h["due"]:
            why.append(f"{h['failed']} of {h['due']} boards failed")
        if h["kept"] >= 5 and h["no_desc"] >= 0.5 * h["kept"]:
            why.append(f"{h['no_desc']} of {h['kept']} kept jobs had no description")
        if h["kept"] >= 5 and h["no_loc"] >= 0.5 * h["kept"]:
            why.append(f"{h['no_loc']} of {h['kept']} kept jobs had no location")
        m = mem.setdefault(prov, {})
        state = "degraded" if why else "ok"
        if m.get("state") != state:
            m.update(state=state, since=today)
        m.update(last=today, why="; ".join(why))
        out[prov] = dict(h, state=state, since=m["since"], why=m["why"])
        if why:
            bad.append(prov)
    return out, bad


def _h(per, b):
    return per.setdefault(b.split(":")[0], {"due": 0, "read": 0, "failed": 0, "drops": 0, "repeated": 0, "kept": 0, "no_desc": 0,
                                            "no_loc": 0, "no_link": 0, "skipped": 0, "new": 0})


def sweep(db, today, stats, get=ats.http, seconds=SWEEP_SECONDS):
    import footprint
    import freshness
    t0 = time.time()
    PRESENT.clear()
    SKIPPED.clear()
    del REJECTED[:]
    run = db["run"] = db.get("run", 0) + 1
    per, drops = {}, []
    scheduled = [b for b, info in db["boards"].items() if due(info, b, today)]
    boards = []
    for b in scheduled:
        if footprint.due(db["boards"][b], run, today):
            boards.append(b)
        else:
            SKIPPED.add(b)
            _h(per, b)["skipped"] += 1
    boards.sort(key=lambda b: priority(db["boards"][b], today))
    rows, borderline, ok_boards, failed = [], [], {}, []

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
        h = _h(per, b)
        if err != "out of time":
            h["due"] += 1
        if jobs is None:
            if err != "out of time":
                info["fails"] = info.get("fails", 0) + 1
                failed.append(b)
                h["failed"] += 1
                footprint.after_failure(info, err, run)
            continue
        h["read"] += 1
        jobs, complete, note = guard(info, jobs)
        suspect = note.startswith("sudden drop")
        if note == "repeated pages":
            h["repeated"] += 1
        if suspect:
            h["drops"] += 1
            drops.append(f"{b}: {note}")
        info.update(ok=today, fails=0, polls=info.get("polls", 0) + 1)
        ok_boards[b] = complete
        if complete:
            PRESENT[b] = freshness.board_keys(jobs) | {j["id"] for j in jobs}
        if not suspect:
            info["jobs"] = len(jobs)
            places = [geo.resolve(j["location"], j.get("remote")).status for j in jobs]
            info["nyc"] = sum(1 for p in places if p in ("local", "remote_us"))
            info["quiet"] = 0 if any(p in ("local", "remote_us", "maybe") for p in places) else info.get("quiet", 0) + 1
        verdicts = [(j, judge(j)) for j in jobs]
        hits = [j for j, v in verdicts if v == "keep"]
        borderline += [j for j, v in verdicts if v == "borderline"]
        if len(REJECTED) < 3000:                  # backlog.POOL_CAP
            REJECTED.extend((b, j) for j, v in verdicts if v == "drop" and j.get("_drop") and len(j.get("description") or "") >= 300)
        if hits:
            info["hits"] = info.get("hits", 0) + len(hits)
            info["last_hit"] = today
        if jobs and jobs[0].get("company") and info.get("company") in ("", b.split(":", 1)[1]):
            info["company"] = jobs[0]["company"]
        h["new"] += footprint.after_read(info, jobs, hits, run, today, suspect)
        for j in hits:
            rows.append(j)
            h["kept"] += 1
            h["no_desc"] += 0 if (j.get("description") or "").strip() else 1
            h["no_loc"] += 0 if (j.get("location") or "").strip() else 1
            h["no_link"] += 0 if str(j.get("url") or "").startswith("http") else 1
    health, degraded = adapter_health(db, today, per)
    stats.update(boards_total=len(db["boards"]), boards_due=len(boards), boards_ok=len(ok_boards), boards_failed=len(failed),
                 failed_sample=failed[:8], sweep_seconds=round(time.time() - t0, 1), borderline=len(borderline),
                 boards_hot=sum(1 for i in db["boards"].values() if i.get("last_hit") and (date.fromisoformat(today) - date.fromisoformat(i["last_hit"])).days <= 14),
                 drops=len(drops), drops_sample=drops[:8], adapter_health=health, adapters_degraded=degraded,
                 footprint=footprint.report(per, len(SKIPPED), run))
    return rows, ok_boards, borderline


def to_row(j, company, today):
    site = j["id"].split(":")[0]
    desc = (j.get("description") or "").replace("\r", "")[:DESC_CAP].replace("\n", "\\n")
    p = geo.resolve(j["location"], j.get("remote"), j.get("description") or "")
    return {"id": j["id"], "site": site, "title": j["title"], "company": company or j.get("company", ""), "location": j["location"],
            "is_remote": "True" if j.get("remote") else "False", "date_posted": j.get("posted", ""), "job_url": j["url"], "job_url_direct": j["url"],
            "min_amount": str(j.get("pay_min") or ""), "max_amount": str(j.get("pay_max") or ""), "interval": j.get("interval") or "",
            "description": desc, "first_seen": today, "last_seen": today, "also_on": "", "region": geo.label(p), "source_ids": j["id"]}
