# Feed audit

2026-11-24T00:26+00:00 · **WARN** · Published, with warnings.

## WARN: 1 job ids now belong to a different employer than last run.

- What the feed did: Kept the new value.
- What to do: An id must stay with one opening; check the source.
  - rippling:74ccd84a-eba3-484f-aa83-0c58d93efa9f: now Tidal Financial Group

## INFO: 623 jobs are shown from earlier runs (279 job-site listings past the 3-day search window, 225 on unchanged boards not due for a read, 77 missed by this run only, 42 not shown by their source for 2+ days); the oldest was last confirmed 2026-11-20.

- What the feed did: Kept for up to 4 days after they were last seen; the confirm column says which, and 2+ days unconfirmed holds a job at C.
  - Bank of America: Business Control Associate (last 2026-11-22)
  - Cleary Gottlieb Steen & Hamilton LLP: Business Development Specialist – Real Estate and Restructuring (last 2026-11-21)
  - Empire State Realty Trust: Capital Cost Specialist (last 2026-11-22)
  - Amazon.com: Partner Solutions Architect, Global Partner Tech (last 2026-11-22)
  - Intuit: Senior Staff Product Designer (AI-Native) (last 2026-11-22)

## INFO: 59 sets of rows share employer, title, place and description.

- What the feed did: Kept apart: employers sometimes post several seats with one description.
- What to do: If the tracker shows these twice, tell Claude so identity can merge them.
  - li-4472496048 / li-4472484601 (Optimum: Sr. FP&A Analyst)
  - in-bc27f38585888f14 / in-58abc5c712c14033 (Consumer Tech: Senior FP&A Analyst)
  - in-924aa1310a45ee85 / li-4492533851 (UPS: FP&A Analyst | 202)
  - li-4459391435 / li-4458310029 (WSP in the U.S.: Project Financial Advisor)
  - li-4474864698 / greenhouse:8265444 (DoorDash: Senior Associate, Finance & Strategy - Deal Desk)
