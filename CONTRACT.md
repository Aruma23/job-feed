# Job record contract

Contract version: 8 (Nov 9, 2026). `feed_status.json` → `contract.version` says which version a run wrote. Every component (this
feed, the tracker's finder and gate, the Hub, the Mac runner) reads and writes jobs by these rules. `tests/test_v6.py` fails if a
column in `scrape.py` is missing here or a value falls outside its list.

## Who owns what

| Owner | Fields | Rule |
|---|---|---|
| Feed (this repo, public) | everything in `jobs.csv` | Facts about the posting: where it came from, its history, how fresh and how verified it is. Never anything about the person using it. |
| Tracker (private Sheet) | lead state and decisions: unseen, reviewed, saved, approved, applied, unknown, rejected, closed; skip reasons; packages; approvals | The feed never writes these. A feed refresh may update posting facts on a lead but never a decision, and never re-opens an applied, skipped or closed lead. |
| Mac runner | submission records | Only it records a submission, and only with positive confirmation. An UNKNOWN is never retried automatically. |

## Identity

- `id` is the canonical ID. A job keeps the `id` it was first published under for life; a closed job that comes back keeps it too (`status` = reappeared), so the tracker sees the same lead, with its decision.
- Company-board jobs use the board's own key (`greenhouse:123`, `workday:R12345`, `workable:AB12`...). Aggregator jobs use the site's posting key (`in-…`, `li-…`).
- `source_ids` lists every ID merged into the row (the same opening seen on several sites). The tracker must treat a lead whose key is any of these as the same job.
- `req_id` is the employer's requisition number when one is known.
- Two openings with the same title at one company stay two rows unless a requisition ID, the employer's link, or company + title + place says they are one.
- `repost_of`: when an opening closes and the same employer + title + place is posted again under a new ID, the new row names the earlier ID. The tracker carries the earlier lead's decision (skipped, applied) instead of showing it as new.

## Columns

Posting: `id`, `site`, `title`, `company`, `location`, `is_remote`, `date_posted`, `job_url`, `job_url_direct`, `min_amount`,
`max_amount`, `interval`, `description`, `also_on`, `region`, `req_id`, `source_ids`.

Lifecycle (history.py): `first_seen`, `last_seen`, `status` (new | changed | reappeared | unchanged; new/changed/reappeared only
for 3 days), `changes` (the latest real change in words), `last_change`, `age_days`. A job is closed only when its board was read
completely without it or its own page answers 404/410; closed jobs leave the feed. A re-dated ad with nothing else changed is a
"refreshed" event in history.json, not a change.

Fit and requirements (relevance.py, reqs.py): `fit_score` (0-100), `fit_category`, `fit_why`, `fit_by` (rules | model),
`skills`, `years_min`, `education`, `seniority`, `pay_low`, `pay_high` (yearly dollars).

Freshness (freshness.py):
- `freshness`: fresh (≤ 7 days) | active (≤ 30, or a real change in the last 14) | aging (≤ 60) | stale (> 60) | gone (the employer's own board, read completely, no longer lists it).
- `ghost_risk`: low | medium | high. Age alone is at most medium; high needs two signals (120+ days, evergreen wording, reposted 2+ times, re-dated 3+ times without change) or "gone".
- `freshness_why`, `reposts` (count, 180 days), `repost_of`.

Employer trust (trust.py):
- `trust`: verified (the employer's own site links to the board, or the job is on the employer's own domain, or `domains.txt` says good) | consistent (the board's name matches the employer, or the employer's listing applies through it) | unverified (nothing confirms it: the default, never a rejection) | suspicious (applicant-directed scam wording, a lookalike or chat-app apply link, or `domains.txt` says bad).
- `trust_why`.

Priority (priority.py): `priority` (A | B | C | D), `priority_score` (0-100), `priority_why`. It orders work and never decides:
the tracker's gate makes every accept/reject call. D also marks what the gate would likely reject, suspicious jobs and jobs gone
from the employer's board. High ghost risk, 5+ years required (a figure the posting marks preferred doesn't count; `years_min`
still carries the most demanding figure), or a fit under 50 caps it at C.

Level realism (priority.py, v8): `level_fit` = in_range | stretch | out_of_range, read for an early-career seat (analyst, senior
analyst, associate; up to `LEVEL_TARGET_YEARS` = 3 years). out_of_range (director+, an internship, a senior manager, principal,
associate director, AVP or controller title, a manager seat asking 4+ years, or 5+ years required) caps priority at C; stretch (a
manager title without a 4+ year ask, any title asking 4 years) caps it at B. Level never raises a job; fit orders jobs inside
the range. It describes the seat, not a person.

Who posted it (poster.py, v8): `poster` = employer (no sign of a third party: the default, not proof) | recruiter (a recruiting
or staffing firm, by its name, or because most of its postings say they are written for a client) | recruiter_unnamed (a
recruiter posting where the hiring company is described or called confidential instead of named). `poster_why`. Separate from
`trust`: a staffing firm's own board is verified and still a recruiter posting. Recruiters are never D for being recruiters;
recruiter loses 3 points, recruiter_unnamed loses 8 and is capped at C because the real employer can't be checked (companies
to avoid, companies already applied to) until it is known. Consulting and advisory wording about the firm's own clients
("on behalf of our clients", "confidential client information") is not evidence.

Employer intelligence (intel.py, v7): `employer_signal` (what this employer's fitting postings show together: openings, posted in
the last 14 days vs the 14 before, teams with 2+ openings, roles hired more than once, pay or title changes; empty when nothing
meets a threshold) and `related_ids` (up to 5 other fitting openings at the same employer, best tier first). employers.json has
the full list.

Self-audit (audit.py, v7): every run is checked before publishing (AUDIT.md, `feed_status.json` → `audit`: level ok | warn | block,
`complete`, items with what happened, what the feed did, what to do). A run that lost many jobs with no confirmed closing restores
them from the last published feed and is marked `complete: false` and `partial: true`; the tracker must not close leads on it.

## Invariants (tested)

1. The same opening from several sources is one row with one `id`. Before publishing, `fold_late` folds an aggregator copy into
   the one board row with the same employer and title whose places agree (same place, one of the board's listed places, the
   same metro label, or a remote board job); several board seats with one title, conflicting requisition IDs or links, or a
   second posting from the same site keep rows apart. `feed_status.json` → `direct.aggregator_rows_folded_late` counts them.
2. Re-running the feed never creates a duplicate row or a new `id` for a known job, and never touches tracker decisions.
3. A closed job can't come back as a new opportunity: the same ID returns as "reappeared"; a new ID for the same opening carries `repost_of`.
4. A failed, partial or suspiciously small board read closes nothing (the last good snapshot is kept); an empty read of a board
   that listed jobs lately is suspicious whatever its size, until it repeats. A job still on its board that stops matching is
   dropped as reclassified, never recorded as closed.
5. Unknown employers and sites are "unverified", not rejected; only scam signals or a confirmed-bad domain make a job "suspicious".
6. Jobs never vanish silently: anything that leaves the feed is closed, aged out, merged, held for review, reclassified or
   trimmed; otherwise the audit restores it and marks the run incomplete.

7. Lack of review capacity never drops a job: a held job stays queued (boards.json `held`) until it is reviewed or shown gone
   (its board read in full without it, its own page gone, or its board removed from the list).

Review state (v8.1, not part of the jobs.csv contract): `classify_cache.json` `usage` (requests and tokens for the current
Pacific day, the daily ceiling, whether a daily limit was hit, 14 days of history) and `audit` (the false-negative sample by
day); `feed_status.json` `classifier` adds `usage`, `reused`, `held_waiting`, `held_oldest_days` and `false_negatives`
(sampled, reviewed, fits, 7-day totals, and `flag` when more than 1 in 8 sampled rejects were fits); `direct.held` counts what
the saved queue carried, closed or dropped, and `direct.held_oldest_days` the longest wait.

## Still to do in the tracker (engine)

Key leads by `id` for all twelve board systems and match on `source_ids`; carry decisions across `repost_of`; read `freshness`,
`trust`, `priority` into Leads; keep "suspicious" and "gone" jobs out of auto-queue; show `feed_status.json` → `direct.adapter_health`
and `coverage` in the Hub.
