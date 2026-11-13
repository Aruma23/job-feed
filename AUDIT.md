# Feed audit

2026-11-13T00:19+00:00 · **WARN** · Published, with warnings.

## WARN: 1738 jobs are shown from earlier runs because this run couldn't confirm them; the oldest was last confirmed 2026-11-09.

- What the feed did: Kept for up to 4 days after they were last seen.
  - Confidential: Sr. FP&A Analyst (SaaS required) - New York (Hybrid - 140K) (last 2026-11-12)
  - Happen Bank: Sr Compliance Data Analyst (last 2026-11-09)
  - Ann Taylor: Customer Data & Insights Analyst (last 2026-11-09)
  - Research Foundation for Mental Hygiene, Inc.: Financial Analyst (last 2026-11-10)
  - Visa: Senior Manager (last 2026-11-09)

## WARN: 1 job ids now belong to a different employer than last run.

- What the feed did: Kept the new value.
- What to do: An id must stay with one opening; check the source.
  - in-d617165c63566858: now BREEZE-EASTERN

## WARN: 4 accounting-heavy jobs ranked A or B.

- What the feed did: Left as published.
- What to do: Fit and category disagree; the relevance rules need a look.
  - HLW International: Specialty Studios Project & Operations Coordinator
  - Turner & Townsend: Financial Operations and Data Analytics Specialist
  - Robert Half: Senior Accounting Analyst
  - Planned Parenthood: Senior Specialist, Budget Administration

## INFO: 78 sets of rows share employer, title, place and description.

- What the feed did: Kept apart: employers sometimes post several seats with one description.
- What to do: If the tracker shows these twice, tell Claude so identity can merge them.
  - in-ebc161ad49e831b0 / in-2b7dffd3f318ac17 (Citi: IT Business Lead Analyst, Vice President)
  - li-4486252149 / li-4487416748 (Northbound Executive Search: Human Resources Business Partner, Technology)
  - in-cfc2aa98682372b3 / in-ef5de0bb7eecb268 (Uber: Manager, Global Delivery Partner Enablement)
  - in-5a55cab70806efdf / in-5725da902cd0097f (Uber: Senior Associate, Global Commercial Enablement Seismic Content & Learning Platforms)
  - li-4463130325 / li-4490381049 (Lantern: Director, Healthcare Economics)

## INFO: 1 entry-level titles ask for 5+ years.

- What the feed did: Ranked by the years asked.
  - everai: Junior Performance Marketer (Remote - Malta) (10 yrs)
