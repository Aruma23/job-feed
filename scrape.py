"""Job feed for Gabriel's Job Search Tracker (v3, Nov 1 2026; v2 Oct 31).

Runs on GitHub Actions four times a day. Pulls public job listings (no sign-in to any account)
from Indeed, LinkedIn, Glassdoor, ZipRecruiter and Google Jobs for NYC finance and planning
roles and writes jobs.csv. The job finder in the Google Sheet reads jobs.csv by column name,
filters it with the same rules as the company boards, and adds new matches to Leads.

v2 vs v1
- More sources and search terms. LinkedIn rows now carry the description and the company's
  own apply link (v1 had neither, so every LinkedIn job was manual and unscored).
- One failing source never empties the feed: jobs from earlier runs are kept for a few days,
  so a blocked run doesn't drop anything.
- The same job from two sites appears once. A job keeps the id it was first published with,
  so the finder never sees it as new twice.
- feed_status.json records what each search returned or why it failed, and whether the run
  was partial.

v3 (Nov 1): jobs straight from employers' own boards (discover.py, ats.py). The feed keeps a growing list of verified
Greenhouse, Lever, Ashby, SmartRecruiters and Workday boards (boards.json), learned from the aggregator rows' links
(short and tracking links opened), from company names, from seeds.txt and weekly from Common Crawl, and reads each board
directly. Direct rows carry the board's own job ID ("greenhouse:123"), the same key the tracker's finder uses, so a job
seen both ways is one lead. A direct job that leaves its board leaves the feed at once; if a board can't be read, its jobs
are kept. Aggregator rows that match a direct job get its link. feed_status.json gains a "direct" section.

v6 (Nov 1): the job record contract (CONTRACT.md). New columns, all computed from what the feed already reads:
- freshness.py: freshness, ghost_risk, freshness_why, reposts, repost_of (an aggregator job whose employer board, read in full,
  no longer lists it is "gone").
- trust.py: trust, trust_why (employer verification and scam screening; unknown is "unverified", never a rejection).
- priority.py: priority (A-D), priority_score, priority_why. Orders the work; the tracker's gate still decides.
- discover.py: a board that suddenly returns far fewer jobs keeps its last good snapshot; per-system health in feed_status.
- feed_status "coverage": the employer apply sites the feed can't read yet, by qualifying jobs, to pick the next adapter.

v7 (Nov 1): audit.py checks every run before it is published (AUDIT.md, feed_status "audit"): jobs that vanished without a
confirmed closing are restored and the run marked incomplete; coverage drops, unconfirmed jobs, identity and priority problems are
reported with what to do. intel.py adds employer_signal and related_ids, and employers.json. A board job still listed but no
longer matching is dropped as reclassified, not recorded as closed.

v4 (Nov 1):
- Location: geo.py's commute regions (NYC, Westchester, Long Island, Rockland, North and Central Jersey, Fairfield County)
  replace the seven-name list; remote-US eligibility is decided separately. Column "region". Rows the tracker's finder would
  still drop by its old list get a "(New York metro: ...)" hint in "location".
- Identity: identity.py. Requisition or posting ID, then the employer's own link, then company + title + location. Two
  openings with the same title at one company stay two rows. Columns "req_id" and "source_ids" (every source ID merged in).
- Relevance: relevance.py. Columns "fit_score", "fit_category", "fit_why", "fit_by" (rules, or model after review).
  review.json lists what the classify job should review, including unconventional company-board titles held back.
- Searches: scheduler.py decides which aggregator searches run (search_stats.json): empty sites and searches that bring no
  new qualified jobs run less often; their slots go to new phrases on trial and to the most productive searches.
"""
import csv
import json
import math
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import freshness
import geo
import audit
import history
import identity
import intel
import priority
import relevance
import reqs
import scheduler
import trust

LOCATION = "New York, NY"
DISTANCE_MILES = 30          # covers Jersey City, Hoboken and Stamford
HOURS_OLD = 72               # postings from the last 3 days (the best odds)
KEEP_DAYS = 4                # keep a job this long after it was last seen, so a failed run loses nothing
DESC_CAP = 6000

# (site, search, results). Each search is tried on its own; a failure skips only that search.
SEARCHES = [
    ("indeed", "FP&A analyst", 60),
    ("indeed", "financial planning and analysis", 60),
    ("indeed", "financial analyst", 60),
    ("indeed", "strategic finance", 60),
    ("indeed", "corporate finance analyst", 40),
    ("indeed", "business finance analyst", 40),
    ("indeed", "workforce planning analyst", 40),
    ("indeed", "headcount planning", 30),
    ("indeed", "business planning analyst", 40),
    ("indeed", "finance associate", 40),
    ("indeed", "investment analyst", 40),
    ("indeed", "portfolio analyst", 30),
    ("indeed", "economic consulting analyst", 30),
    ("linkedin", "FP&A analyst", 25),
    ("linkedin", "financial analyst", 25),
    ("linkedin", "strategic finance", 25),
    ("linkedin", "workforce planning", 20),
    ("glassdoor", "FP&A analyst", 30),
    ("glassdoor", "financial analyst", 30),
    ("glassdoor", "strategic finance", 30),
    ("zip_recruiter", "FP&A analyst", 30),
    ("zip_recruiter", "financial analyst", 30),
    ("google", "FP&A analyst jobs near New York, NY since 3 days ago", 30),
    ("google", "strategic finance analyst jobs near New York, NY since 3 days ago", 30),
    ("google", "financial planning analyst jobs near New York, NY since 3 days ago", 30),
]
COLS = ["id", "site", "title", "company", "location", "is_remote", "date_posted",
        "job_url", "job_url_direct", "min_amount", "max_amount", "interval", "description",
        "first_seen", "last_seen", "also_on",
        "region", "req_id", "source_ids", "fit_score", "fit_category", "fit_why", "fit_by",
        "status", "changes", "last_change", "age_days", "skills", "years_min", "education", "seniority", "pay_low", "pay_high",
        "freshness", "ghost_risk", "freshness_why", "reposts", "repost_of", "trust", "trust_why", "priority", "priority_score",
        "priority_why", "employer_signal", "related_ids"]
CONTRACT_VERSION = 7
QUALIFY_MIN = 50             # rules score for a job to count as qualified in the search stats
REVIEW_MAX = 400


def clean(value):
    """Turns pandas blanks into empty text and keeps every value on one line."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r", "").replace("\n", "\\n")


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def ident(row):
    """Kept for older callers: the fallback part of identity.py's layered identity."""
    return identity.fallback_key(row)


def quality(row):
    """Which copy of a duplicate to keep: one with the company's own apply link and a description."""
    return (1 if row.get("job_url_direct") else 0) + (1 if row.get("description") else 0)


def run_search(scrape, site, term, n):
    kw = dict(site_name=[site], location=LOCATION, distance=DISTANCE_MILES, results_wanted=n,
              hours_old=HOURS_OLD, country_indeed="USA", description_format="markdown", verbose=0)
    if site == "google":
        kw["google_search_term"] = term
    else:
        kw["search_term"] = term
    if site == "linkedin":
        kw["linkedin_fetch_description"] = True   # description + the company's own apply link
    df = scrape(**kw)
    return [] if df is None else df.to_dict("records")


HINT = re.compile(r" ?\((?:New York metro: [^()]*|Remote, US)\)$")


def load_previous(path):
    try:
        csv.field_size_limit(10 ** 8)
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    for r in rows:                        # the finder hint is added at write time only
        r["location"] = HINT.sub("", r.get("location") or "")
    return rows


def merge(previous, fresh, today, index_out=None):
    """Combine this run's jobs with recent ones from earlier runs.

    - A job keeps the id it was first published under (the finder keys leads by it).
    - Copies of the same opening collapse into one row (identity.py); the best copy's links and description win, and every
      source ID is kept in source_ids. Different openings with the same title stay separate.
    - Jobs not seen this run stay until KEEP_DAYS after they were last seen.
    """
    idx = identity.Index()
    for p in previous:
        p = {c: p.get(c, "") for c in COLS}
        p["first_seen"] = p["first_seen"] or today
        p["last_seen"] = p["last_seen"] or p["first_seen"]
        p["source_ids"] = ";".join(identity.source_ids(p))
        if idx.by.get("id:" + p["id"]):
            continue
        idx.add(p)
    for f in fresh:
        slot = idx.find(f)
        if slot is None:
            f = dict(f, first_seen=today, last_seen=today, also_on="", source_ids=f["id"])
            f.pop("_new", None)
            idx.add(f)
            continue
        old = idx.rows[slot]
        sites = set(filter(None, (old.get("also_on") or "").split(";"))) | {old["site"], f["site"]}
        best = f if quality(f) > quality(old) else old
        merged = dict(best)
        merged.update(id=old["id"], site=old["site"], first_seen=old["first_seen"], last_seen=today,
                      also_on=";".join(sorted(sites - {old["site"]})),
                      source_ids=";".join(dict.fromkeys(identity.source_ids(old) + [f["id"]])))
        for c in ("job_url_direct", "description", "min_amount", "max_amount", "interval"):
            if not merged.get(c):
                merged[c] = f.get(c) or old.get(c) or ""
        idx.update(slot, merged)
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()
    return [r for r in idx.rows if r["last_seen"] >= cutoff]


def verdict(row, cache=None):
    """Rules verdict, replaced by a cached model review of the same title and description."""
    import discover as D
    v = cache and relevance.cached(cache, row)
    if v:
        return dict(v, review=False)
    return relevance.rules(row.get("title"), str(row.get("description") or "").replace("\\n", "\n"), D.title_kind(row.get("title")))


def qualifies(row):
    p = geo.resolve(row.get("location"), str(row.get("is_remote")) == "True")
    return p.status in ("local", "remote_us") and verdict(row)["score"] >= QUALIFY_MIN


def collect(scrape, searches=None, pause=(4, 9), sleep=time.sleep, previous=None):
    """Runs each search. Status per search: rows returned, unique rows, qualified rows, and qualified rows new to the feed."""
    searches = SEARCHES if searches is None else searches
    fresh, seen_urls, status = [], set(), []
    known = identity.Index()
    for p in previous or []:
        known.add(p)
    for site, term, n in searches:
        entry = {"site": site, "search": term, "results": n, "returned": 0, "new": 0, "qualified": 0, "qnew": 0, "error": ""}
        try:
            records = run_search(scrape, site, term, n)
        except Exception as exc:  # a blocked search must not stop the others
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            records = []
        entry["returned"] = len(records)
        for rec in records:
            url = rec.get("job_url")
            if not url or url in seen_urls or not rec.get("title"):
                continue
            seen_urls.add(url)
            row = {c: clean(rec.get(c)) for c in COLS[:13]}
            row["site"] = row["site"] or site
            row["description"] = row["description"][:DESC_CAP]
            fresh.append(row)
            entry["new"] += 1
            if qualifies(row):
                entry["qualified"] += 1
                if known.find(row) is None:
                    entry["qnew"] += 1
                    known.add(row)
        status.append(entry)
        print(f"{site} / {term}: {entry['new']} new, {entry['qnew']} new qualified" + (f" (skipped: {entry['error']})" if entry["error"] else ""))
        sleep(random.uniform(*pause))
    return fresh, status


NATIVE = re.compile(r"^(greenhouse|lever|ashby|workday|smartrecruiters|workable|breezy|recruitee|teamtailor|pinpoint|rippling|personio):")
STILL_OPEN_CHECKS = 40       # jobs missing from a partial board read, checked on their own page each run
DIRECT_CAP = 1500


def direct_step(agg_rows, prev_direct, today, db_path, get=None, resolve=None, cc_text=None, weekday=None, range_get=None):
    """Grows the board list, reads every board, and returns (direct rows, borderline rows, stats). Never raises: a failed
    step is noted."""
    import ats
    import discover as D
    get = get or ats.http
    resolve = resolve or ats.resolve_redirect
    stats = {"boards_new": 0, "version": "v5"}
    db = D.load_db(db_path)
    try:
        better = D.from_rows(db, agg_rows, today, stats, resolve)
        for r in agg_rows:
            if r.get("id") in better:
                r["job_url_direct"] = better[r["id"]]
    except Exception as e:
        stats["links_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    try:
        names = [r.get("company") for r in agg_rows if qualifies(r)] + [r.get("company") for r in agg_rows if D.title_kind(r.get("title"))]
        sd = D.seeds(os.path.join(os.path.dirname(db_path) or ".", D.SEEDS))
        cur = db.get("seed_cursor", 0) % max(1, len(sd)) if sd else 0
        D.probe_names(db, names, today, stats, get, budget=15)
        D.probe_names(db, sd[cur:cur + 20], today, stats, get, budget=20)
        db["seed_cursor"] = cur + 20
    except Exception as e:
        stats["probe_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    try:
        import harvest
        crawl_list = (lambda: [c["id"] for c in json.loads(cc_text("https://index.commoncrawl.org/collinfo.json"))]) if cc_text else None
        harvest.step(db, os.path.dirname(db_path) or ".", today, stats, D.place_ok, D.title_kind, D.add_board, get, range_get, crawl_list)
    except Exception as e:
        stats["harvest_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    jobs, ok_boards, border = D.sweep(db, today, stats, get)
    rows, held, prev, owner = {}, {}, {r["id"]: r for r in prev_direct}, {}

    def put(j, target):
        board = ats.board_of(j["url"])
        if j["id"] in owner and owner[j["id"]].lower() == board.lower() and owner[j["id"]] != board:
            return                                              # one board stored under two spellings: the same job
        if j["id"] in owner and owner[j["id"]] != board:        # the same Workday req number at two employers
            j = dict(j, id=j["id"] + "~" + board.split(":", 1)[1].split("|")[0])
        owner[j["id"]] = board
        r = D.to_row(j, (db["boards"].get(board) or {}).get("company"), today)
        r["_board"] = board
        if j["id"] in prev:
            r["first_seen"] = prev[j["id"]].get("first_seen") or today
        target[j["id"]] = r

    for j in jobs:
        put(j, rows)
        if j.get("unconventional"):
            stats["unconventional"] = stats.get("unconventional", 0) + 1
    for j in border:
        if j["id"] not in rows:
            put(j, held)
    kept_old, closed, checks, still, reclassified = 0, [], 0, 0, []
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()
    for k, r in prev.items():
        if k in rows or k in held:
            continue
        b = ats.board_of(r.get("job_url"))
        if b in ok_boards and ok_boards[b]:          # the whole board was read and the job isn't on it: closed
            if k in D.PRESENT.get(b, ()):           # still listed, just no longer a match: not a closing
                reclassified.append(k)
                continue
            closed.append(k)
            continue
        if b in ok_boards and checks < STILL_OPEN_CHECKS:   # partial read (Workday searches): ask the job's own page
            checks += 1
            v = ats.still_open(r.get("job_url"), get)
            if v is False:
                closed.append(k)
                continue
            if v:
                still += 1
                rows[k] = dict(r, last_seen=today)
                continue
        if r.get("last_seen", today) >= cutoff:        # board unreadable, or no answer: keep it a while
            rows[k] = r
            kept_old += 1
    ranked = sorted(rows.values(), key=lambda r: (r.get("date_posted") or r.get("first_seen") or ""), reverse=True)
    out = ranked[:DIRECT_CAP]
    stats["_explained"] = [r["id"] for r in ranked[DIRECT_CAP:]] + reclassified + list(held)
    stats["reclassified"] = len(reclassified)
    stats["rows_direct"] = len(out)
    stats["kept_unread"] = kept_old
    stats["closed_confirmed"] = len(closed)
    stats["still_open_checked"] = checks
    stats["still_open"] = still
    stats["requests"] = {k: v for k, v in sorted(ats.STATS.items(), key=lambda kv: -kv[1]["requests"])[:12]}
    stats["new_today"] = sum(1 for r in out if r.get("first_seen") == today)
    stats["employers"] = len({D.emp_key(r.get("company")) for r in out})
    try:
        names = {b: (i.get("company") or "") for b, i in db["boards"].items()}
        stats["routes_gone"] = 0
        for r in agg_rows:
            g = freshness.route_gone(r, D.PRESENT, names)
            if g:
                r["_gone"] = g
                stats["routes_gone"] += 1
        trust.learn_domains(db, agg_rows + out, today, os.path.join(os.path.dirname(db_path) or ".", trust.OVERRIDES))
        trust.verify_employers(db, out + [r for r in agg_rows if qualifies(r)], today, stats, get)
    except Exception as e:
        stats["verify_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    D.save_db(db, db_path)
    return out, list(held.values()), stats, closed


def annotate(agg_rows, direct_rows):
    """An aggregator row for a job also found on the company's own board is folded into that board row: the board row gains the
    site in also_on, the aggregator id in source_ids, and pay it lacked. Matched by the board's job link or requisition ID, else
    by company + title + location when exactly one board job fits. Returns the folded aggregator ids (contract invariant 1)."""
    import discover as D
    by_key, by_ctl = {}, {}
    for r in direct_rows:
        for k in identity.url_keys(r) | identity.req_keys(r):
            by_key.setdefault(k, r)
        ck = (D.emp_key(r["company"]), D.title_key(r["title"]), geo.loc_key(r["location"], r.get("is_remote") == "True"))
        by_ctl.setdefault(ck, []).append(r)
    folded = set()
    for r in agg_rows:
        d = next((by_key[k] for k in sorted(identity.url_keys(r) | identity.req_keys(r)) if k in by_key), None)
        if not d:
            cands = by_ctl.get((D.emp_key(r.get("company")), D.title_key(r.get("title")), geo.loc_key(r.get("location"), r.get("is_remote") == "True")), [])
            d = cands[0] if len(cands) == 1 else None
        if not d:
            continue
        sites = set(filter(None, (d.get("also_on") or "").split(";"))) | {r.get("site")} | set(filter(None, (r.get("also_on") or "").split(";")))
        d["also_on"] = ";".join(sorted(sites - {d.get("site"), ""}))
        d["source_ids"] = ";".join(dict.fromkeys(identity.source_ids(d) + identity.source_ids(r)))
        if r.get("first_seen") and r["first_seen"] < (d.get("first_seen") or "9"):
            d["first_seen"] = r["first_seen"]
        if not (d.get("min_amount") or d.get("max_amount")) and (r.get("min_amount") or r.get("max_amount")):
            d.update(min_amount=r.get("min_amount", ""), max_amount=r.get("max_amount", ""), interval=r.get("interval", ""))
        folded.add(r["id"])
    return folded


def finish(rows, cache):
    """Region, finder hint, requisition ID, source IDs and fit for every row. Returns the review queue (row ids)."""
    queue = []
    for r in rows:
        p = geo.resolve(r.get("location"), str(r.get("is_remote")) == "True", r.get("description") or "")
        r["region"] = geo.label(p)
        r["location"] = geo.finder_location(r.get("location") or "", p)
        r["source_ids"] = ";".join(identity.source_ids(r))
        r["req_id"] = identity.req_label(identity.req_keys(r))
        v = verdict(r, cache)
        relevance.apply(r, v)
        r.update(reqs.requirements(r.get("title"), r.get("description")))
        r["pay_low"], r["pay_high"] = reqs.annual_pay(r)
        if v.get("review"):
            queue.append(r)
    import discover as D
    rank = {"ambiguous": 0, "strong": 1, "broad": 2}
    queue.sort(key=lambda r: r.get("date_posted") or r.get("first_seen") or "", reverse=True)    # newest first ...
    queue.sort(key=lambda r: rank.get(D.title_kind(r.get("title")), 3))                         # ... within each kind
    return [r["id"] for r in queue][:REVIEW_MAX]


DIRECT_DEFAULT = True


def main(scrape=None, out="jobs.csv", status_path="feed_status.json", now=None, direct=None, get=None, resolve=None, cc_text=None, weekday=None,
         range_get=None):
    if scrape is None:
        from jobspy import scrape_jobs as scrape
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    here = os.path.dirname(out) or "."
    previous_all = load_previous(out)
    previous = [r for r in previous_all if not NATIVE.match(r.get("id") or "")]
    prev_direct = [r for r in previous_all if NATIVE.match(r.get("id") or "")]
    cache = relevance.load_cache(os.path.join(here, "classify_cache.json"))
    stats_path = os.path.join(here, "search_stats.json")
    sched = scheduler.load(stats_path)
    searches, decisions = scheduler.plan(sched, SEARCHES, POOL if POOL_ON else [], today)
    fresh, status = collect(scrape, searches, previous=previous_all)
    scheduler.record(sched, status, today)
    scheduler.save(sched, stats_path)
    failed = [s for s in status if s["error"]]
    by_site = {}
    for s in status:
        d = by_site.setdefault(s["site"], {"searches": 0, "failed": 0, "new": 0, "qualified_new": 0})
        d["searches"] += 1
        d["failed"] += 1 if s["error"] else 0
        d["new"] += s["new"]
        d["qualified_new"] += s["qnew"]
    # JobSpy logs most blocks and returns nothing instead of raising, so a site where every
    # search came back empty also counts as a partial run.
    empty_sites = sorted(k for k, d in by_site.items() if d["new"] == 0)
    rows = merge(previous, fresh, today)
    direct = DIRECT_DEFAULT if direct is None else direct
    direct_rows, held, dstats, closed = [], [], {"version": "v5", "skipped": True}, []
    if direct and os.environ.get("NO_DIRECT") != "1":
        try:
            direct_rows, held, dstats, closed = direct_step(rows, prev_direct, today, os.path.join(here, "boards.json"), get, resolve, cc_text, weekday, range_get)
            folded = annotate(rows, direct_rows)
            rows = [r for r in rows if r["id"] not in folded]
            dstats["aggregator_rows_folded"] = len(folded)
        except Exception as exc:   # the aggregator part of the feed never fails because of the direct part
            dstats = {"version": "v5", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            direct_rows = prev_direct
    # unconventional board titles held back: in if an earlier review rescued them, otherwise queued for review
    rescued = [h for h in held if (relevance.cached(cache, h) or {}).get("score", 0) >= relevance.MODEL_RESCUE
               and relevance.cached(cache, h)["category"] != relevance.ACCT]
    held_ids = {h["id"] for h in rescued}
    direct_rows = direct_rows + rescued
    waiting = [h for h in held if h["id"] not in held_ids and not relevance.cached(cache, h)]
    total = len(rows) + len(direct_rows)
    dstats["rows_aggregator"] = len(rows)
    dstats["pct_direct"] = round(100 * len(direct_rows) / total) if total else 0
    dstats["held_for_review"] = len(waiting)
    dstats["rescued_by_review"] = len(rescued)
    rows = rows + direct_rows
    queue = finish(rows, cache)
    hist = history.load(os.path.join(here, "history.json"))
    hstats = history.update(hist, rows, today, closed)
    history.save(hist, os.path.join(here, "history.json"))
    contract = annotate_v6(rows, hist, here, today)
    import discover as D
    intel_summary = intel.annotate(rows, today, os.path.join(here, "employers.json"), D.NEVER)
    explained = set(closed) | set(dstats.pop("_explained", [])) | {h["id"] for h in held}
    now_counts = {"rows_by_site": _count(audit._site(r) for r in rows), "boards": {"boards read": dstats.get("boards_ok", 0)}}
    log_path = os.path.join(here, "audit_log.json")
    log = audit.load_log(log_path)
    n_before = len(rows)
    rows, report = audit.run(previous_all, rows, explained, today, KEEP_DAYS, log, now_counts)
    for r in rows[n_before:]:
        r["location"] = geo.finder_location(r.get("location") or "", geo.resolve(r.get("location"), str(r.get("is_remote")) == "True"))
    audit.save_log(log, dict(now_counts, ran=now.isoformat(timespec="seconds"), level=report["level"]), log_path)
    with open(os.path.join(here, "AUDIT.md"), "w", encoding="utf-8") as f:
        f.write(audit.markdown(report, now.isoformat(timespec="minutes")))
    for h in waiting:
        h["region"] = geo.label(geo.resolve(h.get("location"), h.get("is_remote") == "True", h.get("description") or ""))
        relevance.apply(h, verdict(h))
    cats = {}
    for r in rows:
        cats[r["fit_category"]] = cats.get(r["fit_category"], 0) + 1
    summary = {"ran_at": now.isoformat(timespec="seconds"), "partial": bool(failed or empty_sites or not report["complete"]),
               "empty_sites": empty_sites,
               "searches": len(status), "failed": len(failed), "fresh_rows": len(fresh),
               "rows_written": len(rows), "by_site": by_site, "detail": status, "direct": dstats,
               "relevance": {"by_category": cats, "strong_fits": sum(1 for r in rows if int(r["fit_score"] or 0) >= 70),
                             "queued_for_review": len(queue) + len(waiting), "reviewed_by_model": sum(1 for r in rows if r["fit_by"] == "model")},
               "regions": _count(r["region"] for r in rows),
               "history": hstats, "seniority": _count(r["seniority"] for r in rows),
               "scheduler": dict(scheduler.report(sched, today), decisions=decisions),
               "contract": contract, "coverage": coverage(rows), "intel": intel_summary, "audit": report}
    if not fresh:
        summary["note"] = "Nothing came back this run; earlier jobs kept."
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(here, "review.json"), "w", encoding="utf-8") as f:
        json.dump({"review_ids": queue, "borderline": [{c: h.get(c, "") for c in COLS} for h in waiting]}, f)
    print(f"Saved {len(rows)} jobs ({len(fresh)} from this run; {len(failed)} of {len(status)} searches failed; {len(queue) + len(waiting)} queued for review)")
    return summary


def annotate_v6(rows, hist, here, today):
    """Freshness, employer trust and priority for every row (one failure never stops the feed). Returns counts."""
    import discover as D
    out = {"version": CONTRACT_VERSION}
    try:
        db = D.load_db(os.path.join(here, "boards.json"))
        good, bad = trust.load_overrides(os.path.join(here, trust.OVERRIDES))
        index = trust.board_index(db)
    except Exception as e:
        db, good, bad, index = {"boards": {}}, {}, {}, {}
        out["trust_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    for r in rows:
        try:
            freshness.assess(r, hist["jobs"].get(r.get("id")), history.family(hist, r), today, r.pop("_gone", ""))
            trust.assess(r, db, good, bad, today, index)
            priority.assess(r)
        except Exception as e:
            out["errors"] = out.get("errors", 0) + 1
            out["last_error"] = f"{r.get('id')}: {type(e).__name__}: {str(e)[:100]}"
        r.pop("_board", None)
    for col in ("freshness", "ghost_risk", "trust", "priority"):
        out[col] = _count(r.get(col, "") for r in rows)
    out["reposted"] = sum(1 for r in rows if r.get("repost_of"))
    out["tier_A_fresh_verified"] = sum(1 for r in rows if r.get("priority") == "A" and r.get("trust") == "verified")
    return out


def after_review(rows, added, feed_dir, today):
    """The classify job changes fit scores and adds rescued board jobs: those get freshness and trust, and every row's priority
    is recomputed from its final fit. Used by relevance.main; never raises."""
    try:
        db = json.load(open(os.path.join(feed_dir, "boards.json"), encoding="utf-8"))
    except (OSError, ValueError):
        db = {"boards": {}}
    good, bad = trust.load_overrides(os.path.join(feed_dir, trust.OVERRIDES) if os.path.exists(os.path.join(feed_dir, trust.OVERRIDES))
                                     else trust.OVERRIDES)
    index = trust.board_index(db)
    d0 = datetime.fromisoformat(today).date()
    for r in added:
        try:
            dp = str(r.get("date_posted") or "")[:10]
            r["age_days"] = str((d0 - date.fromisoformat(dp)).days) if re.match(r"\d{4}-\d{2}-\d{2}$", dp) else "0"
            freshness.assess(r, None, {}, today)
            trust.assess(r, db, good, bad, today, index)
        except Exception:
            pass
    for r in rows:
        try:
            priority.assess(r)
        except Exception:
            pass


def coverage(rows, top=15):
    """Employer apply sites the feed can't read directly yet, ranked by qualifying aggregator jobs (fit 50+) and employers."""
    import ats
    import urllib.parse
    hosts = {}
    for r in rows:
        if NATIVE.match(r.get("id") or ""):
            continue
        u = r.get("job_url_direct") or ""
        if not u:
            hosts.setdefault("(no employer link)", [0, 0, set()])[0] += 1
            continue
        if ats.board_of(u):
            continue
        h = trust.registered(urllib.parse.urlsplit(u).hostname or "")
        e = hosts.setdefault(h, [0, 0, set()])
        e[0] += 1
        if int(r.get("fit_score") or 0) >= QUALIFY_MIN:
            e[1] += 1
            e[2].add(r.get("company"))
    ranked = sorted(hosts.items(), key=lambda kv: (-kv[1][1], -len(kv[1][2]), -kv[1][0]))[:top]
    return {"uncovered_hosts": [{"host": h, "rows": a, "qualified": q, "employers": len(es)} for h, (a, q, es) in ranked],
            "aggregator_rows_unreadable": sum(v[0] for v in hosts.values())}


def _count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


POOL = scheduler.POOL
POOL_ON = True


if __name__ == "__main__":
    main()
