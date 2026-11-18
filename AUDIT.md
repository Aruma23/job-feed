# Feed audit

2026-11-18T17:47+00:00 · **WARN** · Published, with warnings.

## WARN: Read 4134 of the 8709 company boards due this run.

- What the feed did: Boards not read keep their jobs for a few days.
- What to do: Check direct.adapter_health for failing or late systems.

## WARN: 2 job ids now belong to a different employer than last run.

- What the feed did: Kept the new value.
- What to do: An id must stay with one opening; check the source.
  - workday:UMG-27982-1: now Universal Music Group
  - workday:R153863: now Northern Trust Corp.

## INFO: 780 jobs are shown from earlier runs (614 job-site listings past the 3-day search window, 165 not shown by their source for 2+ days, 1 on unchanged boards not due for a read); the oldest was last confirmed 2026-11-13.

- What the feed did: Kept for up to 4 days after they were last seen; the confirm column says which, and 2+ days unconfirmed holds a job at C.
  - Fox Corporation: Freelance Senior Producer, Fox Nation (last 2026-11-16)
  - Montclair State University: Director of Accounts Payable (last 2026-11-16)
  - Personetics: Director Content & Corporate Marketing (last 2026-11-16)
  - Pfizer: Director, Operational Excellence (last 2026-11-16)
  - Prudential: Associate Actuary (Retirement) (last 2026-11-16)

## INFO: 45 sets of rows share employer, title, place and description.

- What the feed did: Kept apart: employers sometimes post several seats with one description.
- What to do: If the tracker shows these twice, tell Claude so identity can merge them.
  - li-4489055544 / li-4491173566 (Aequor: Financial Analyst I (Hybrid 1-2 days onsite on W2 ), SAC experience)
  - in-7785496c982ebe27 / in-044766370fb2a448 (Ripple: Product Marketing Manager)
  - li-4491043235 / li-4489728927 (Shiseido: Analyst, Finance, FP&A)
  - li-4473146667 / li-4473491546 / greenhouse:4718610006 / greenhouse:4718607006 (CoreWeave: Manager, Strategic Finance)
  - li-4491063782 / li-4491240461 (Steve Madden: Sr. Finance Manager, NYC)
