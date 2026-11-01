"""Job feed for Gabriel's Job Search Tracker (v2, Oct 31 2026).

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
"""
import csv
import json
import math
import random
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

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
        "first_seen", "last_seen", "also_on"]


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
    """Same company + same title = same job, whichever site it came from."""
    company = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|the)\b", " ", norm(row.get("company")))
    return re.sub(r"\s+", " ", company).strip() + "|" + norm(row.get("title"))


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


def load_previous(path):
    try:
        csv.field_size_limit(10 ** 8)
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def merge(previous, fresh, today):
    """Combine this run's jobs with recent ones from earlier runs.

    - A job keeps the id it was first published under (the finder keys leads by it).
    - Duplicates across sites collapse into one row; the best copy's links and description win.
    - Jobs not seen this run stay until KEEP_DAYS after they were last seen.
    """
    by_ident, order = {}, []
    for p in previous:
        p = {c: p.get(c, "") for c in COLS}
        p["first_seen"] = p["first_seen"] or today
        p["last_seen"] = p["last_seen"] or p["first_seen"]
        k = ident(p)
        if k in by_ident:
            continue
        by_ident[k] = p
        order.append(k)
    for f in fresh:
        k = ident(f)
        old = by_ident.get(k)
        if old is None:
            f = dict(f, first_seen=today, last_seen=today, also_on="")
            by_ident[k] = f
            order.append(k)
            continue
        sites = set(filter(None, (old.get("also_on") or "").split(";"))) | {old["site"], f["site"]}
        keep_id, keep_site, first = old["id"], old["site"], old["first_seen"]
        best = f if quality(f) > quality(old) else old
        merged = dict(best)
        merged.update(id=keep_id, site=keep_site, first_seen=first, last_seen=today,
                      also_on=";".join(sorted(sites - {keep_site})))
        for c in ("job_url_direct", "description", "min_amount", "max_amount", "interval"):
            if not merged.get(c):
                merged[c] = f.get(c) or old.get(c) or ""
        by_ident[k] = merged
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()
    return [by_ident[k] for k in order if by_ident[k]["last_seen"] >= cutoff]


def collect(scrape, searches=None, pause=(4, 9), sleep=time.sleep):
    searches = SEARCHES if searches is None else searches
    fresh, seen_urls, status = [], set(), []
    for site, term, n in searches:
        entry = {"site": site, "search": term, "returned": 0, "new": 0, "error": ""}
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
        status.append(entry)
        print(f"{site} / {term}: {entry['new']} new" + (f" (skipped: {entry['error']})" if entry["error"] else ""))
        sleep(random.uniform(*pause))
    return fresh, status


def main(scrape=None, out="jobs.csv", status_path="feed_status.json", now=None):
    if scrape is None:
        from jobspy import scrape_jobs as scrape
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    previous = load_previous(out)
    fresh, status = collect(scrape)
    failed = [s for s in status if s["error"]]
    by_site = {}
    for s in status:
        d = by_site.setdefault(s["site"], {"searches": 0, "failed": 0, "new": 0})
        d["searches"] += 1
        d["failed"] += 1 if s["error"] else 0
        d["new"] += s["new"]
    # JobSpy logs most blocks and returns nothing instead of raising, so a site where every
    # search came back empty also counts as a partial run.
    empty_sites = sorted(k for k, d in by_site.items() if d["new"] == 0)
    rows = merge(previous, fresh, today)
    summary = {"ran_at": now.isoformat(timespec="seconds"), "partial": bool(failed or empty_sites),
               "empty_sites": empty_sites,
               "searches": len(status), "failed": len(failed), "fresh_rows": len(fresh),
               "rows_written": len(rows), "by_site": by_site, "detail": status}
    if not fresh:
        summary["note"] = "Nothing came back this run; earlier jobs kept."
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Saved {len(rows)} jobs ({len(fresh)} from this run; {len(failed)} of {len(status)} searches failed)")
    return summary


if __name__ == "__main__":
    main()
