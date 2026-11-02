"""Offline tests for v9: responsibility-based trial searches (finance work under other titles)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scheduler as S  # noqa: E402
import scrape  # noqa: E402
import discover as D  # noqa: E402
import relevance as R  # noqa: E402


class DutySearches(unittest.TestCase):
    NEW = ["revenue strategy", "commercial finance", "finance and strategy manager", "business planning manager",
           "business operations forecasting budget", "strategy operations forecast budget variance", "pricing strategy financial model",
           "strategy and operations finance"]

    def test_pool_has_duty_searches_on_trial_only(self):
        terms = {t for _, t, _ in S.POOL}
        for t in self.NEW:
            self.assertIn(t, terms)
        base = {(s, t) for s, t, _ in scrape.SEARCHES}
        for site, term, n in S.POOL:
            self.assertNotIn((site, term), base, "trial searches are not in the base list")
            self.assertTrue(site in ("indeed", "linkedin", "glassdoor", "zip_recruiter", "google") and 0 < n <= 100)
        self.assertEqual(len(S.POOL), len(set((s, t) for s, t, _ in S.POOL)), "no duplicate trial searches")

    def test_new_trials_are_admitted_when_there_is_room(self):
        state = {"v": 1, "run": 0, "searches": {}, "sites": {}}
        seen = set()
        for _ in range(12):
            searches, _ = S.plan(state, scrape.SEARCHES, S.POOL, "2026-11-02")
            for s in searches:
                seen.add(s[1])
                e = state["searches"].setdefault(S.key(s[0], s[1]), {"days": {}, "runs_total": 0})
                e["runs_total"] = e.get("runs_total", 0) + 1
            state["run"] += 1
        self.assertTrue(any(t in seen for t in self.NEW), "duty searches get tried")

    def test_unconventional_titles_are_judged_by_duties(self):
        fpa = ("Own the annual budget and monthly forecast, variance analysis versus plan, the P&L review and scenario planning. "
               "Partner with sales leadership on pricing and revenue forecasting.")
        for title in ("Revenue Strategy Manager", "Business Operations Manager", "Finance & Strategy Manager", "Commercial Finance Lead"):
            r = R.rules(title, fpa, D.title_kind(title))
            self.assertGreaterEqual(r["score"], 50, title)
            self.assertNotEqual(r["category"], R.ACCT, title)
        ops = "Vendor onboarding, warehouse logistics, procurement and customer support tickets."
        self.assertLess(R.rules("Business Operations Manager", ops, D.title_kind("Business Operations Manager"))["score"], 50)


if __name__ == "__main__":
    unittest.main()
