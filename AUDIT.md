# Feed audit

2026-11-28T04:56+00:00 · **WARN** · Published, with warnings.

## WARN: 2 job ids now belong to a different employer than last run.

- What the feed did: Kept the new value.
- What to do: An id must stay with one opening; check the source.
  - lever:e7e603a1-b6fc-4fe7-9ab5-15b8a3eb6ab8: now Portage Point Partners
  - lever:9339a108-3505-4daa-8d15-a3bd2571cb08: now Portage Point Partners

## INFO: 1 indeed job(s) left the feed without a confirmed closing.

- What the feed did: Left out (too few to suggest a broken reader).
  - Voya Financial: IM Entry Level Analyst Program - Product Management Analyst

## INFO: 561 jobs are shown from earlier runs (207 on unchanged boards not due for a read, 202 job-site listings past the 3-day search window, 86 missed by this run only, 66 not shown by their source for 2+ days); the oldest was last confirmed 2026-11-24.

- What the feed did: Kept for up to 4 days after they were last seen; the confirm column says which, and 2+ days unconfirmed holds a job at C.
  - Optimum: Sr. FP&A Analyst (last 2026-11-25)
  - SESI Consulting Engineers: Accounting Assistant I (last 2026-11-25)
  - B&G Foods, Inc.: Inventory Analyst (last 2026-11-26)
  - LSEG (London Stock Exchange Group): Senior Threat Hunting Analyst (GSOC) (last 2026-11-26)
  - JPMorganChase: Applied AI/ML Data Scientist - Data & Analytics - Associate (last 2026-11-25)

## INFO: 59 sets of rows share employer, title, place and description.

- What the feed did: Kept apart: employers sometimes post several seats with one description.
- What to do: If the tracker shows these twice, tell Claude so identity can merge them.
  - li-4472496048 / li-4472484601 (Optimum: Sr. FP&A Analyst)
  - li-4474864698 / greenhouse:8265444 (DoorDash: Senior Associate, Finance & Strategy - Deal Desk)
  - in-40532b7920502b9d / in-aaf6048bc9e0b5fb (Genpact: Business Development Executive)
  - li-4492325190 / smartrecruiters:3743990017021876 (City of New York: Administrative Budget Analyst)
  - li-4493668366 / li-4495502571 (Plona Partners: Senior Financial Reporting Analyst)
