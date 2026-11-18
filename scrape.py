"""Job feed for a job search tracker (v3, Nov 1 2026; v2 Oct 31).

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

v8 (Nov 1): footprint.py. Each board keeps an access profile (fingerprint, unchanged streak, read interval, new fitting jobs
per read, fields carried, failure kinds); unchanged boards are read less often (up to every 2 runs with a recent match, 8
otherwise, never more than 2 days apart), productive ones every run; sources.json maps employer -> site -> board -> public
endpoint -> fitting jobs.

v8.1 (Nov 1, Phase 1 of the cost and decision-quality audit): backlog.py. Held jobs are saved in boards.json "held" and carried
until reviewed or shown gone (board read in full without them, own page gone, board removed); they are reviewed most promising
first, with waiting time adding so nothing starves. Jobs the rules turn away on their description are pooled and a 16-job
sample goes to the review each run. relevance.py: Flash-Lite only, a daily request ceiling shared fairly across runs, stop on
any rate-limit reply, token counts, 8 jobs a request, one review per posting across ids and sources.

v10 (Nov 14, contract 8): an audit of where the feed learns and where it doesn't, answered in code.
- Search credit no longer depends on list order: a job found by k searches gives each 1/k (scheduler.py), counted with the
  model's cached verdicts, and corrected on the next run from the published verdict of jobs that were waiting for review.
- Mined search phrases: titles of the feed's own good jobs that no current search would match become trial searches
  (mined_pool), capped and still made to earn their place.
- employer.py: employer_type (direct, staffing, search firm, marketplace, undisclosed, unknown) with the evidence.
- confirm (freshness.py): current | not_due | window | recent | unconfirmed, so "not reconfirmed" says why.
- The company-board cap keeps qualified jobs first (and spreads places across employers) instead of the newest 1,500.
- priority.py holds at C: accounting-heavy, intermediary or unnamed employer, unconfirmed 2+ days.
- relevance.py: model verdicts carry PROMPT_VERSION (old ones re-reviewed lazily when it changes); the model's strengths must
  appear in the posting.

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
import backlog
import footprint
import history
import employer
import identity
import intel
import priority
import relevance
import reqs
import scheduler
import target
import textfit
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
        "priority_why", "employer_signal", "related_ids", "employer_type", "employer_type_why", "confirm",
        "fit_evidence"]   # v9: appended, so no existing column moves
CONTRACT_VERSION = 9
# Posting facts a re-read of the same copy may change (merge): what the employer edits. Identity and lifecycle columns never.
UPDATABLE = ("title", "company", "location", "is_remote", "date_posted", "job_url", "job_url_direct", "min_amount", "max_amount",
             "interval", "description")
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


def _tracking(url):
    """A link that only redirects to the posting (ContactHR, appcast, recruitics...): ats.REDIRECTS or an rx_url wrapper."""
    import ats
    return bool(ats.REDIRECTS.match(url) or "rx_url=" in url)


def merge(previous, fresh, today, index_out=None):
    """Combine this run's jobs with recent ones from earlier runs.

    - A job keeps the id it was first published under (the finder keys leads by it).
    - Copies of the same opening collapse into one row (identity.py); the best copy's links and description win, and every
      source ID is kept in source_ids. Different openings with the same title stay separate.
    - A copy read again from the same source brings the posting's current facts (title, pay, text, dates, links): the
      employer's edits replace what was stored, so history.py sees them as a change. A blank field never erases a known value.
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
        # v10: the same site posting key is the same posting, whatever else changed. find() can call two copies different
        # openings when their employer links differ (a listing whose apply link moved to another ATS URL); it then added a
        # second row under the same id, and the run's output check threw the whole run away (Nov 14, 12:13 am and 7:14 am).
        slot = next((s for s in idx.by.get("id:" + f["id"], []) if f["id"] in identity.source_ids(idx.rows[s])), None)
        if slot is None:
            slot = idx.find(f)
        if slot is None:
            f = dict(f, first_seen=today, last_seen=today, also_on="", source_ids=f["id"], _confirm="current")
            f.pop("_new", None)
            idx.add(f)
            continue
        old = idx.rows[slot]
        sites = set(filter(None, (old.get("also_on") or "").split(";"))) | {old["site"], f["site"]}
        # The row's own copy read again (the canonical id) is the posting as it stands now: its edits win over what was
        # stored, a blank never erases a known value. Any other copy is another site's rendering of the same opening, so it
        # only fills gaps unless it is the better copy (a direct apply link and a description); letting every copy overwrite
        # would make the title and text flip between sites from run to run and show up as changes.
        again = f["id"] == old["id"]
        best = f if quality(f) > quality(old) else old
        merged = dict(best)
        if again:
            merged.update({c: f[c] for c in UPDATABLE if f.get(c)})
            od = old.get("job_url_direct") or ""
            if od and not _tracking(od) and _tracking(f.get("job_url_direct") or ""):
                merged["job_url_direct"] = od       # v10: the employer link resolved from an earlier copy beats the site's
                                                   # tracking link (ContactHR re-sent every run undid JPMorgan's fold-in)
        merged.update(id=old["id"], site=old["site"], first_seen=old["first_seen"], last_seen=today, _confirm="current",
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


def qualifies(row, cache=None):
    """Right place and relevant: the rules, or the model's cached verdict on the same posting when there is one (v10: the
    scheduler is credited with the feed's final verdicts, not only the rules')."""
    p = geo.resolve(row.get("location"), str(row.get("is_remote")) == "True")
    return p.status in ("local", "remote_us") and verdict(row, cache)["score"] >= QUALIFY_MIN and not _above_safe(row)


def final_qualified(row):
    """A published row's final verdict (after the model's review): right place and fit_score at or above the bar."""
    p = geo.resolve(row.get("location"), str(row.get("is_remote")) == "True")
    try:
        fit = int(float(row.get("fit_score") or 0))
    except ValueError:
        fit = 0
    return p.status in ("local", "remote_us") and fit >= QUALIFY_MIN


def collect(scrape, searches=None, pause=(4, 9), sleep=time.sleep, previous=None, cache=None, pending=None):
    """Runs each search. Status per search: rows returned, unique rows, qualified rows, and qualified rows new to the feed.

    v10 (Nov 14): credit no longer depends on the order of the search list. Every search that returned a job is recorded
    against it; a qualified job new to the feed gives each of the k searches that found it 1/k of a discovery ("qnew", the
    Shapley value of the coverage game), and "excl" counts the ones only that search found (its pure incremental value).
    "qualified" counts every qualified job a search returned, duplicates included. Rows whose rules verdict is queued for the
    model are added to `pending` (id -> [search keys, provisional verdict]) so the next run can correct the credit with the
    published, model-reviewed verdict (scheduler.settle)."""
    searches = SEARCHES if searches is None else searches
    fresh, status = [], []
    known = identity.Index()
    for p in previous or []:
        known.add(p)
    run_idx, found, url_slot = identity.Index(), [], {}   # this run's openings; found[slot]: which searches returned it
    for site, term, n in searches:
        entry = {"site": site, "search": term, "results": n, "returned": 0, "usable": 0, "new": 0, "qualified": 0, "qnew": 0, "excl": 0, "error": ""}
        k = f"{site}|{term}"
        try:
            records = run_search(scrape, site, term, n)
        except Exception as exc:  # a blocked search must not stop the others
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            records = []
        entry["returned"] = len(records)
        for rec in records:
            url = rec.get("job_url")
            if not url or not rec.get("title"):
                continue
            entry["usable"] += 1
            if url in url_slot:
                slot = url_slot[url]
            else:                                  # a new link this run: kept for merge(), which folds copies of one job
                row = {c: clean(rec.get(c)) for c in COLS[:13]}
                row["site"] = row["site"] or site
                row["description"] = textfit.fit(row["description"], DESC_CAP)
                fresh.append(row)
                entry["new"] += 1
                slot = run_idx.find(row)           # the same opening under another site's link shares one credit
                if slot is None:
                    slot = run_idx.add(row)
                    v = verdict(row, cache)
                    found.append({"keys": [], "q": qualifies(row, cache), "new": known.find(row) is None,
                                  "review": bool(v.get("review")) and v.get("by") == "rules", "id": row["id"]})
                url_slot[url] = slot
            if k in found[slot]["keys"]:
                continue
            found[slot]["keys"].append(k)
            if found[slot]["q"]:
                entry["qualified"] += 1
        status.append(entry)
        sleep(random.uniform(*pause))
    by_key = {f"{s['site']}|{s['search']}": s for s in status}
    for f in found:
        share = 1 / len(f["keys"])
        if f["q"] and f["new"]:
            for k in f["keys"]:
                by_key[k]["qnew"] += share
            if len(f["keys"]) == 1:
                by_key[f["keys"][0]]["excl"] += 1
        if f["new"] and f["review"] and pending is not None:
            pending[f["id"]] = [f["keys"], int(f["q"])]
    for s in status:
        s["qnew"] = round(s["qnew"], 3)
        print(f"{s['site']} / {s['search']}: {s['new']} new, {s['qnew']} new qualified ({s['excl']} only here)"
              + (f" (skipped: {s['error']})" if s["error"] else ""))
    return fresh, status


NATIVE = re.compile(r"^(greenhouse|lever|ashby|workday|smartrecruiters|workable|breezy|recruitee|teamtailor|pinpoint|rippling|personio|oracle):")
STILL_OPEN_CHECKS = 40       # jobs missing from a partial board read, checked on their own page each run
DIRECT_CAP = 1500


def same_tenant(a, b):
    """Two board keys of one employer account: the same system and tenant ("workday:pwc|wd3|US_Experienced_Careers" and
    "workday:pwc|wd3|Global_Experienced_Careers"), spelling aside. A job id seen on both is one job."""
    ka, _, ra = a.partition(":")
    kb, _, rb = b.partition(":")
    return ka == kb and ra.split("|")[0].lower() == rb.split("|")[0].lower()


def split_seeds(lines):
    """seeds.txt lines -> (company names to probe, [(board key, company)] to add as they are). A board line starts with a
    known system ("workday:tenant|wd3|Site = Company"); everything else is a name."""
    import ats
    names, boards = [], []
    for ln in lines:
        key, _, company = ln.partition(" = ")
        kind = key.split(":", 1)[0]
        if ":" in key and kind in ats.FETCH and " " not in key.strip():
            boards.append((key.strip(), company.strip()))
        else:
            names.append(ln)
    return names, boards


def direct_step(agg_rows, prev_direct, today, db_path, get=None, resolve=None, cc_text=None, weekday=None, range_get=None, cache=None):
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
        sd, seed_boards = split_seeds(D.seeds(os.path.join(os.path.dirname(db_path) or ".", D.SEEDS)))
        for key, company in seed_boards:              # boards named exactly in seeds.txt ("workday:pwc|wd3|Site = PwC")
            D.add_board(db, key, company, "seed-board", today, stats)
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
        if j["id"] in owner and owner[j["id"]] != board and same_tenant(owner[j["id"]], board):
            return                     # one employer's job on two of its sites (PwC US and global), or one board in two spellings
        if j["id"] in owner and owner[j["id"]] != board:        # the same Workday req number at two employers
            j = dict(j, id=j["id"] + "~" + board.split(":", 1)[1].split("|")[0])
        owner[j["id"]] = board
        r = D.to_row(j, (db["boards"].get(board) or {}).get("company"), today)
        r["_board"] = board
        r["_confirm"] = "current"
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
    reviewed_ids = (cache or {}).get("jobs") or {}
    for b, j in D.REJECTED:                         # turned away by the rules but already rated by the model (the false-negative
        if j["id"] in reviewed_ids and j["id"] not in rows and j["id"] not in held:   # sample): held, so a fit stays rescued
            put(j, held)
    kept_old, closed, checks, still, reclassified, kept_skipped = 0, [], 0, 0, [], 0
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()
    for k, r in prev.items():
        if k in rows or k in held:
            continue
        b = ats.board_of(r.get("job_url"))
        if b in D.SKIPPED or b in D.NOT_DUE or (r.get("_board") and (
                r["_board"] in D.SKIPPED or r["_board"] in D.NOT_DUE)):
            rows[k] = dict(r, _confirm="not_due")   # board was deliberately not scheduled/read this run
            kept_skipped += 1
            continue
        if "~" in k and k.split("~")[0] in rows and rows[k.split("~")[0]].get("_board", "").lower() == ats.board_of(r.get("job_url")).lower():
            reclassified.append(k)                  # the same job read earlier under a second spelling of its board
            continue
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
                rows[k] = dict(r, last_seen=today, _confirm="current")
                continue
        if r.get("last_seen", today) >= cutoff:        # board unreadable, or no answer: keep it a while
            rows[k] = r
            kept_old += 1
    held_ok = True
    try:                                            # the saved review queue: carried until reviewed or shown to be gone
        stats["held"] = backlog.carry(db, held, rows, ok_boards, D.SKIPPED, D.PRESENT, today, ats.board_of,
                                      lambda u: ats.still_open(u, get) if get else None)
        pool = [(b, j) for b, j in D.REJECTED if j["id"] not in rows and j["id"] not in held and j["id"] not in reviewed_ids]
        pick = backlog.sample(pool, backlog.AUDIT_SAMPLE, f"{today}|{db.get('run', '')}")
        stats["_audit"] = [dict(D.to_row(j, (db["boards"].get(b) or {}).get("company"), today), _board=b, _drop=j.get("_drop", ""))
                           for b, j in pick]
        stats["rejects_pooled"] = len(pool)
    except Exception as e:
        held_ok = False
        stats["held_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    ranked = cap_rank(list(rows.values()), cache)
    out = ranked[:DIRECT_CAP]
    stats["cap"] = cap_report(ranked, DIRECT_CAP, cache)
    stats["_explained"] = [r["id"] for r in ranked[DIRECT_CAP:]] + reclassified + list(held)
    stats["reclassified"] = len(reclassified)
    stats["rows_direct"] = len(out)
    stats["kept_unread"] = kept_old
    stats["kept_not_due"] = kept_skipped
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
    if held_ok:                                     # on an error the saved queue is left as it was
        stats["held_saved"] = backlog.store(db, held, lambda r: bool(cache and relevance.cached(cache, r)), today, COLS)
    try:
        stats["read_age"] = D.read_age(db, today)
    except Exception as e:
        stats["read_age"] = {"error": f"{type(e).__name__}: {e}"}
    D.save_db(db, db_path)
    return out, list(held.values()), stats, closed


CAP_PER_EMPLOYER = 40       # past this many, an employer's further jobs rank after every other employer's (diversity)


def _cap_value(r, cache):
    v = verdict(r, cache)
    sc = int(v.get("score") or 0)
    acct = str(v.get("category") or "").startswith("Accounting")
    return 2 if sc >= QUALIFY_MIN and not acct else 1 if sc >= 35 and not acct else 0, sc


def cap_rank(rows, cache=None):
    """Which company-board jobs keep a place under DIRECT_CAP (v10). Before: the newest 1,500, whatever they were. Now: qualified
    jobs (fit 50+, not accounting-heavy) first, then near-misses (35+), then the rest; within each, an employer's first
    CAP_PER_EMPLOYER jobs before its others, then newest first. Unconventional titles are not at risk here: they wait in the
    held queue (backlog.py), which the cap doesn't touch."""
    import discover as D
    vals = {id(r): _cap_value(r, cache) for r in rows}
    by_date = sorted(rows, key=lambda r: (r.get("date_posted") or r.get("first_seen") or ""), reverse=True)
    seen, rank_in_emp = {}, {}
    for r in sorted(by_date, key=lambda r: -vals[id(r)][0]):
        k = (D.emp_key(r.get("company")), vals[id(r)][0])
        seen[k] = seen.get(k, 0) + 1
        rank_in_emp[id(r)] = seen[k]
    order = {id(r): i for i, r in enumerate(by_date)}
    return sorted(rows, key=lambda r: (-vals[id(r)][0], rank_in_emp[id(r)] > CAP_PER_EMPLOYER, order[id(r)]))


def cap_report(ranked, cap, cache=None):
    cut = ranked[cap:]
    vals = [_cap_value(r, cache) for r in cut]
    return {"cap": cap, "candidates": len(ranked), "trimmed": len(cut), "trimmed_qualified": sum(1 for b, _ in vals if b == 2),
            "trimmed_avg_fit": round(sum(sc for _, sc in vals) / len(vals), 1) if vals else 0,
            "note": ("raise DIRECT_CAP: qualified jobs were trimmed" if any(b == 2 for b, _ in vals) else "")}


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


def _above_safe(row, fresh=False):
    return bool(target.off_safe(row, fresh))


def level_gate(rows):
    """(kept, left out): rows outside the target profile (target.off: level, or more required years than the profile reaches)
    leave the feed. Counted in feed_status "target" -> gate."""
    kept, out, why = [], [], {}
    for r in rows:
        w = target.off_safe(r)
        if w:
            out.append(r)
            why[w.split(":")[0]] = why.get(w.split(":")[0], 0) + 1
        else:
            kept.append(r)
    LEVEL_GATE.clear()
    LEVEL_GATE.update(left_out=len(out), by_reason=why, by_level=_count(r.get("seniority", "") for r in out),
                      by_site=_count(audit._site(r) for r in out),
                      share_of_collected=round(len(out) / max(1, len(rows)), 3))
    return kept, out


LEVEL_GATE = {}


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
        if not (r["pay_low"] or r["pay_high"]):            # v10: the range the posting states in its text (NY pay transparency)
            r["pay_low"], r["pay_high"] = reqs.pay_from_text(r.get("description"))
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
    settled = scheduler.settle(sched, previous_all, today, final_qualified)   # last run's credit, corrected by the final verdicts
    searches, decisions = scheduler.plan(sched, SEARCHES, (POOL if POOL_ON else []) + mined_pool(previous_all, sched), today)
    pending = {}
    fresh, status = collect(scrape, searches, previous=previous_all, cache=cache, pending=pending)
    scheduler.remember(sched, pending, today)
    scheduler.record(sched, status, today)
    scheduler.save(sched, stats_path)
    failed = [s for s in status if s["error"]]
    by_site = {}
    for s in status:
        d = by_site.setdefault(s["site"], {"searches": 0, "failed": 0, "returned": 0, "usable": 0, "new": 0, "qualified_new": 0})
        d["searches"] += 1
        d["failed"] += 1 if s["error"] else 0
        d["returned"] += s["returned"]
        d["usable"] += s["usable"]
        d["new"] += s["new"]
        d["qualified_new"] += s["qnew"]
    # JobSpy logs most blocks and returns nothing instead of raising. Previously-known results are healthy;
    # malformed records without a usable title and URL are not.
    empty_sites = empty_sources(status)
    rows = merge(previous, fresh, today)
    direct = DIRECT_DEFAULT if direct is None else direct
    direct_rows, held, dstats, closed = [], [], {"version": "v5", "skipped": True}, []
    if direct and os.environ.get("NO_DIRECT") != "1":
        try:
            direct_rows, held, dstats, closed = direct_step(rows, prev_direct, today, os.path.join(here, "boards.json"), get, resolve, cc_text, weekday, range_get,
                                                            cache=cache)
            if get is None and os.environ.get("NO_SHADOW") != "1":   # real runs only: new adapters tried, nothing added (shadow.py)
                import ats
                import shadow
                dstats["shadow"] = shadow.run(rows, lambda u, **kw: ats.http(u, tries=1, **kw), resolve or ats.resolve_redirect)
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
    waiting = [h for h in held if h["id"] not in held_ids and not relevance.cached(cache, h) and not target.off_safe(h, fresh=True)]
    import discover as D
    waiting.sort(key=lambda h: (-backlog.value(h, today, relevance.rules(h.get("title"), h.get("description"), D.title_kind(h.get("title")))["core"]),
                                h["id"]))                               # most promising first; waiting time keeps adding
    audit_rows = dstats.pop("_audit", [])
    dstats["held_oldest_days"] = backlog.oldest_wait(waiting, today)
    total = len(rows) + len(direct_rows)
    dstats["rows_aggregator"] = len(rows)
    dstats["pct_direct"] = round(100 * len(direct_rows) / total) if total else 0
    dstats["held_for_review"] = len(waiting)
    dstats["rescued_by_review"] = len(rescued)
    rows = rows + direct_rows
    queue = finish(rows, cache)
    rows, above = level_gate(rows)                  # Nov 18: outside the target profile (target.py) is never published
    gone = {r["id"] for r in above}
    queue = [q for q in queue if (q.get("id") if isinstance(q, dict) else q) not in gone]
    hist = history.load(os.path.join(here, "history.json"))
    hstats = history.update(hist, rows, today, closed)
    history.save(hist, os.path.join(here, "history.json"))
    contract = annotate_v6(rows, hist, here, today)
    import discover as D
    intel_summary = intel.annotate(rows, today, os.path.join(here, "employers.json"), D.NEVER)
    explained = set(closed) | set(dstats.pop("_explained", [])) | {h["id"] for h in held}
    explained |= {r["id"] for r in above} | {p.get("id") for p in previous_all if _above_safe(p, fresh=True)}   # left out on purpose, not lost
    now_counts = {"rows_by_site": _count(audit._site(r) for r in rows), "boards": {"boards read": dstats.get("boards_ok", 0)}}
    if dstats.get("boards_due") is not None:
        now_counts["boards_due"] = dstats["boards_due"]
    log_path = os.path.join(here, "audit_log.json")
    log = audit.load_log(log_path)
    n_before = len(rows)
    rows, report = audit.run(previous_all, rows, explained, today, KEEP_DAYS, log, now_counts)
    for r in rows[n_before:]:
        r["location"] = geo.finder_location(r.get("location") or "", geo.resolve(r.get("location"), str(r.get("is_remote")) == "True"))
        r["confirm"] = freshness.confirm(r, today)              # restored from the last feed: not confirmed by this run
    if len(rows) > n_before:
        employer.annotate(rows[n_before:])
    audit.save_log(log, dict(now_counts, ran=now.isoformat(timespec="seconds"), level=report["level"]), log_path)
    try:
        dstats["sources"] = footprint.source_graph(D.load_db(os.path.join(here, "boards.json")), rows, coverage(rows)["uncovered_hosts"],
                                                   today, os.path.join(here, "sources.json"))
    except Exception as e:
        dstats["sources"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
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
               "regions": _count(r["region"] for r in rows), "description_fit": dict(textfit.STATS),
               "history": hstats, "seniority": _count(r["seniority"] for r in rows),
               "scheduler": dict(scheduler.report(sched, today), decisions=decisions, settled=settled),
               "contract": contract, "coverage": coverage(rows), "intel": intel_summary, "audit": report,
               "target": dict(target.check(rows), gate=dict(LEVEL_GATE)),
               # Third review, Nov 18: "partial" is about lost data; these say which stage finished. The classify job sets
               # "classifier" (None until it runs).
               "complete": {"board_sweep": not (dstats.get("boards_due", 0) or 0) > (dstats.get("boards_ok", 0) or 0) + (dstats.get("boards_failed", 0) or 0),
                            "aggregator": not (failed or empty_sites), "classifier": None}}
    if not fresh:
        summary["note"] = "Nothing came back this run; earlier jobs kept."
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    rows, dup = unique_ids(rows)
    if dup:
        summary["duplicate_ids_merged"] = dup
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(here, "review.json"), "w", encoding="utf-8") as f:
        json.dump({"review_ids": queue,
                   "borderline": [dict({c: h.get(c, "") for c in COLS}, held_since=h.get("held_since") or today) for h in waiting],
                   "held_ids": sorted(h["id"] for h in held),           # so the classify job keeps their verdicts
                   "audit": [dict({c: a.get(c, "") for c in COLS}, _drop=a.get("_drop", "")) for a in audit_rows]}, f)
    print(f"Saved {len(rows)} jobs ({len(fresh)} from this run; {len(failed)} of {len(status)} searches failed; {len(queue) + len(waiting)} queued for review)")
    return summary


def empty_sources(status):
    """Sources with no usable listings. Previously-known results count as healthy; malformed rows do not."""
    by_site = {}
    for entry in status:
        d = by_site.setdefault(entry["site"], {"searches": 0, "failed": 0, "usable": 0})
        d["searches"] += 1
        d["failed"] += bool(entry.get("error"))
        d["usable"] += int(entry.get("usable", entry.get("returned")) or 0)
    return sorted(site for site, d in by_site.items() if d["usable"] == 0 and d["failed"] < d["searches"])


def unique_ids(rows):
    """Last line of defence for CONTRACT invariant 1 (one row per id): a second row under an id already written is folded
    into the first (its source ids kept) instead of failing the run's output check, which throws the whole run away.
    Returns (rows, {"count", "ids"}) so a cause upstream still shows in feed_status."""
    seen, out, ids = {}, [], []
    for r in rows:
        k = r.get("id")
        if k in seen:
            first = seen[k]
            first["source_ids"] = ";".join(dict.fromkeys(identity.source_ids(first) + identity.source_ids(r)))
            ids.append(k)
            continue
        seen[k] = r
        out.append(r)
    return out, ({"count": len(ids), "ids": ids[:10]} if ids else {})


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
        except Exception as e:
            out["errors"] = out.get("errors", 0) + 1
            out["last_error"] = f"{r.get('id')}: {type(e).__name__}: {str(e)[:100]}"
        r["confirm"] = freshness.confirm(r, today)
        r.pop("_board", None)
        r.pop("_confirm", None)
    out["employer_type"] = employer.annotate(rows)
    for r in rows:
        try:
            priority.assess(r)
        except Exception as e:
            out["errors"] = out.get("errors", 0) + 1
            out["last_error"] = f"{r.get('id')}: {type(e).__name__}: {str(e)[:100]}"
    for col in ("freshness", "ghost_risk", "trust", "priority", "confirm"):
        out[col] = _count(r.get(col, "") for r in rows)
    ab = [r for r in rows if r.get("priority") in ("A", "B")]
    out["quality"] = {"a_b": len(ab), "a_b_direct_employer": sum(1 for r in ab if r.get("employer_type") == "direct"),
                      "a_b_intermediary": sum(1 for r in ab if employer.is_intermediary(r)),
                      "a_b_accounting": sum(1 for r in ab if str(r.get("fit_category") or "").startswith("Accounting")),
                      "a_b_confirmed_current": sum(1 for r in ab if r.get("confirm") in ("current", "not_due")),
                      "a_b_by_seniority": _count(r.get("seniority", "") for r in ab),        # third review: the level that ranks
                      "a_b_by_evidence": _count(r.get("fit_evidence", "") for r in ab),
                      "share_direct_employer": round(sum(1 for r in rows if r.get("employer_type") == "direct") / max(1, len(rows)), 3)}
    out["reposted"] = sum(1 for r in rows if r.get("repost_of"))
    out["tier_A_fresh_verified"] = sum(1 for r in rows if r.get("priority") == "A" and r.get("trust") == "verified")
    return out


def after_review(rows, added, feed_dir, today):
    """The classify job changes fit scores and adds rescued board jobs: those get freshness, trust, confirmation, requirements
    and pay (v10: requirements and pay were missing on rescued rows), and every row's employer type and priority are recomputed
    from its final fit. Used by relevance.main; never raises."""
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
            r["_confirm"] = "current"       # these rescued rows came from a board read in this run
            r["confirm"] = freshness.confirm(r, today)
        except Exception:
            pass
        try:                                     # the same requirement and pay fields as every other row (finish())
            r.update(reqs.requirements(r.get("title"), r.get("description")))
            r["pay_low"], r["pay_high"] = reqs.annual_pay(r)
            if not (r["pay_low"] or r["pay_high"]):
                r["pay_low"], r["pay_high"] = reqs.pay_from_text(r.get("description"))
        except Exception:
            pass
    try:
        employer.annotate(rows)
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

# ---------------- v10: search phrases mined from the feed's own good jobs ----------------
MINE_FIT = 70                # a "good" job: final fit 70+, not accounting-heavy, in the commute area
MINE_EMPLOYERS = 3           # a phrase needs this many employers whose good jobs no current search would match
MINE_PRECISION = 0.5         # and at least half of all feed jobs with that title phrase must qualify
_ANCHOR = re.compile(r"\b(financ\w*|fp&a|fpa|planning|budget\w*|forecast\w*|strateg\w*|revenue|treasury|capacity|workforce|headcount|"
                     r"investment|pricing|commercial|portfolio|valuation|economic\w*|business performance|corporate development)\b")
_NO_MINE = re.compile(r"\b(director|vp|vice president|head|chief|intern|internship|summer|accountant|accounting|tax|audit|payroll|"
                      r"engineer|developer|sales|controller|bookkeep\w*|clerk|assistant|manager|recruit\w*)\b")
_LEVEL = re.compile(r"\b(senior|sr|junior|jr|lead|staff|principal|i{1,3}|iv|1|2|3)\b\.?")


def title_core(title):
    """'Sr. Revenue Finance Analyst II, Ads (Hybrid)' -> 'revenue finance analyst'."""
    t = str(title or "").lower()
    t = re.split(r"\s[-–|/]\s|,|\(|\[|:", t)[0]
    t = t.replace(" & ", " and ").replace("&amp;", "&")
    t = _LEVEL.sub(" ", t)
    t = re.sub(r"[^a-z0-9& ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _covers(term, title):
    words = [w for w in re.sub(r"[^a-z0-9& ]+", " ", term.lower().split(" jobs ")[0]).split() if w not in ("and", "of", "the")]
    t = title.lower()
    return bool(words) and all(re.search(r"\b" + re.escape(w) + r"\b", t) for w in words)


def mined_pool(published, sched, limit=4):
    """New search hypotheses from the feed's own good jobs (no model, no cost). A title phrase is proposed when good jobs at
    MINE_EMPLOYERS+ employers carry it, no current search's words would match those titles (Indeed matches every word, so a
    search whose words all appear in a title would have found it), and most feed jobs with that phrase qualify. Proposals go
    to the scheduler as trials on Indeed: they still have to earn their place with new qualified jobs, and at most
    scheduler.MINED_ACTIVE are on trial at once. Returns [(site, term, n, why)], strongest first."""
    try:
        terms = [e.get("term", "") for e in (sched.get("searches") or {}).values()] + [t for _, t, _ in SEARCHES + POOL]
        have = {re.sub(r"\s+", " ", t.lower()).strip() for t in terms}
        good, allc = {}, {}
        for r in published:
            core = title_core(r.get("title"))
            if not (2 <= len(core.split()) <= 5) or not _ANCHOR.search(core) or _NO_MINE.search(core):
                continue
            ok = final_qualified(r)
            a = allc.setdefault(core, [0, 0])
            a[0] += 1
            a[1] += 1 if ok else 0
            try:
                fit = int(float(r.get("fit_score") or 0))
            except ValueError:
                fit = 0
            if ok and fit >= MINE_FIT and not str(r.get("fit_category") or "").startswith("Accounting"):
                if any(_covers(t, r.get("title") or "") for t in terms):
                    continue
                good.setdefault(core, set()).add(identity.emp_key(r.get("company")))
        out = []
        for core, emps in good.items():
            n, q = allc[core]
            if len(emps) < MINE_EMPLOYERS or core in have or q / n < MINE_PRECISION:
                continue
            out.append((len(emps), q / n, core))
        out.sort(reverse=True)
        return [("indeed", core, 30, f"{k} employers no search covers, {round(100 * p)}% of {allc[core][0]} feed jobs qualify")
                for k, p, core in out[:limit]]
    except Exception:
        return []


if __name__ == "__main__":
    main()
