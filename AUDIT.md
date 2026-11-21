# Feed audit

2026-11-21T00:25+00:00 · **WARN** · Published, with warnings.

## WARN: 1 job ids now belong to a different employer than last run.

- What the feed did: Kept the new value.
- What to do: An id must stay with one opening; check the source.
  - workday:JR3112: now Coach

## INFO: 508 jobs are shown from earlier runs (210 on unchanged boards not due for a read, 134 missed by this run only, 120 job-site listings past the 3-day search window, 44 not shown by their source for 2+ days); the oldest was last confirmed 2026-11-18.

- What the feed did: Kept for up to 4 days after they were last seen; the confirm column says which, and 2+ days unconfirmed holds a job at C.
  - David Joseph & Company: Senior Software Engineer (last 2026-11-19)
  - Universal Music Group: Executive Assistant (last 2026-11-18)
  - Haymarket Media: Account Strategist (last 2026-11-18)
  - Valley Bank: Digital Banking Fraud Analyst (last 2026-11-19)
  - Phillips 66: Analyst II, Bayway Digital Area Coordinator (last 2026-11-18)

## INFO: 60 sets of rows share employer, title, place and description.

- What the feed did: Kept apart: employers sometimes post several seats with one description.
- What to do: If the tracker shows these twice, tell Claude so identity can merge them.
  - li-4489728927 / li-4492128312 (Shiseido: Analyst, Finance, FP&A)
  - in-544d01bdafd3cdb7 / workday:JR10036167 / workday:JR10036164 (Genpact: Partner - Finance Advisory)
  - li-4472496048 / li-4472484601 (Optimum: Sr. FP&A Analyst)
  - in-bc27f38585888f14 / in-58abc5c712c14033 (Consumer Tech: Senior FP&A Analyst)
  - in-72bff37bff23c43c / greenhouse:5481842008 (Anthropic: GTM Strategy & Operations - Strategic Planning)
