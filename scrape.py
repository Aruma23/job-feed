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

import geo
import identity
import relevance
import scheduler

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
        "region", "req_id", "source_ids", "fit_score", "fit_category", "fit_why", "fit_by"]
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


NATIVE = re.compile(r"^(greenhouse|lever|ashby|workday|smartrecruiters):")
DIRECT_CAP = 1500


def direct_step(agg_rows, prev_direct, today, db_path, get=None, resolve=None, cc_text=None, weekday=None):
    """Grows the board list, reads every board, and returns (direct rows, borderline rows, stats). Never raises: a failed
    step is noted."""
    import ats
    import discover as D
    get = get or ats.http
    resolve = resolve or ats.resolve_redirect
    stats = {"boards_new": 0, "version": "v4"}
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
        D.cc_step(db, today, stats, get, cc_text, weekday)
    except Exception as e:
        stats["cc_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    jobs, ok_boards, border = D.sweep(db, today, stats, get)
    rows, held, prev, owner = {}, {}, {r["id"]: r for r in prev_direct}, {}

    def put(j, target):
        board = ats.board_of(j["url"])
        if j["id"] in owner and owner[j["id"]] != board:        # the same Workday req number at two employers
            j = dict(j, id=j["id"] + "~" + board.split(":", 1)[1].split("|")[0])
        owner[j["id"]] = board
        r = D.to_row(j, (db["boards"].get(board) or {}).get("company"), today)
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
    kept_old = 0
    for k, r in prev.items():
        if k in rows or k in held:
            continue
        b = ats.board_of(r.get("job_url"))
        if b in ok_boards:            # read fine and the job is gone: closed
            continue
        if r.get("last_seen", today) >= (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat():
            rows[k] = r
            kept_old += 1
    out = sorted(rows.values(), key=lambda r: (r.get("date_posted") or r.get("first_seen") or ""), reverse=True)[:DIRECT_CAP]
    stats["rows_direct"] = len(out)
    stats["kept_unread"] = kept_old
    stats["new_today"] = sum(1 for r in out if r.get("first_seen") == today)
    stats["employers"] = len({D.emp_key(r.get("company")) for r in out})
    D.save_db(db, db_path)
    return out, list(held.values()), stats


def annotate(agg_rows, direct_rows):
    """An aggregator row for a job also found on the company's own board gets that board's link. Matched by the board's job
    link or requisition ID, else by company + title + location when exactly one board job fits."""
    import discover as D
    by_key, by_ctl = {}, {}
    for r in direct_rows:
        for k in identity.url_keys(r) | identity.req_keys(r):
            by_key.setdefault(k, r)
        ck = (D.emp_key(r["company"]), D.title_key(r["title"]), geo.loc_key(r["location"], r.get("is_remote") == "True"))
        by_ctl.setdefault(ck, []).append(r)
    n = 0
    for r in agg_rows:
        d = next((by_key[k] for k in sorted(identity.url_keys(r) | identity.req_keys(r)) if k in by_key), None)
        if not d:
            cands = by_ctl.get((D.emp_key(r.get("company")), D.title_key(r.get("title")), geo.loc_key(r.get("location"), r.get("is_remote") == "True")), [])
            d = cands[0] if len(cands) == 1 else None
        if not d:
            continue
        site = d["site"]
        r["also_on"] = ";".join(sorted(set(filter(None, (r.get("also_on") or "").split(";"))) | {site}))
        r["source_ids"] = ";".join(dict.fromkeys(identity.source_ids(r) + [d["id"]]))
        if not re.search(r"greenhouse\.io|lever\.co|ashbyhq|myworkdayjobs|smartrecruiters", r.get("job_url_direct") or ""):
            r["job_url_direct"] = d["job_url"]
        n += 1
    return n


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
        if v.get("review"):
            queue.append(r)
    import discover as D
    rank = {"ambiguous": 0, "strong": 1, "broad": 2}
    queue.sort(key=lambda r: r.get("date_posted") or r.get("first_seen") or "", reverse=True)    # newest first ...
    queue.sort(key=lambda r: rank.get(D.title_kind(r.get("title")), 3))                         # ... within each kind
    return [r["id"] for r in queue][:REVIEW_MAX]


DIRECT_DEFAULT = True


def main(scrape=None, out="jobs.csv", status_path="feed_status.json", now=None, direct=None, get=None, resolve=None, cc_text=None, weekday=None):
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
    direct_rows, held, dstats = [], [], {"version": "v4", "skipped": True}
    if direct and os.environ.get("NO_DIRECT") != "1":
        try:
            direct_rows, held, dstats = direct_step(rows, prev_direct, today, os.path.join(here, "boards.json"), get, resolve, cc_text, weekday)
            dstats["aggregator_rows_matched"] = annotate(rows, direct_rows)
        except Exception as exc:   # the aggregator part of the feed never fails because of the direct part
            dstats = {"version": "v4", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
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
    for h in waiting:
        h["region"] = geo.label(geo.resolve(h.get("location"), h.get("is_remote") == "True", h.get("description") or ""))
        relevance.apply(h, verdict(h))
    cats = {}
    for r in rows:
        cats[r["fit_category"]] = cats.get(r["fit_category"], 0) + 1
    summary = {"ran_at": now.isoformat(timespec="seconds"), "partial": bool(failed or empty_sites),
               "empty_sites": empty_sites,
               "searches": len(status), "failed": len(failed), "fresh_rows": len(fresh),
               "rows_written": len(rows), "by_site": by_site, "detail": status, "direct": dstats,
               "relevance": {"by_category": cats, "strong_fits": sum(1 for r in rows if int(r["fit_score"] or 0) >= 70),
                             "queued_for_review": len(queue) + len(waiting), "reviewed_by_model": sum(1 for r in rows if r["fit_by"] == "model")},
               "regions": _count(r["region"] for r in rows),
               "scheduler": dict(scheduler.report(sched, today), decisions=decisions)}
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


def _count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


POOL = scheduler.POOL
POOL_ON = True


if __name__ == "__main__":
    main()
