"""Nov 18 2026: one target profile (target.py), every part agrees with it, and each run is counted against it.

The manager-level episode: a third of the published feed was above the target level while every test passed, because the
level was written in five places and no test compared them. These tests fail when a part drifts from target.py.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import discover as D  # noqa: E402
import relevance as R  # noqa: E402
import reqs  # noqa: E402
import scheduler as S  # noqa: E402
import scrape  # noqa: E402
import target  # noqa: E402
import watchdog as W  # noqa: E402

DUTIES = ("Own the monthly forecast and the annual budget. Prepare variance analysis against plan and present it to "
          "business partners. Build driver-based models for headcount and revenue. ")


def row(title="Senior Financial Analyst", extra="", **k):
    return dict({"title": title, "company": "Acme", "description": DUTIES * 2 + extra}, **k)


class EveryPartReadsTheSameProfile(unittest.TestCase):
    def test_board_filter_and_gate_agree_on_every_title_word(self):
        for w in target.TITLE_WORDS_OUT:
            t = f"FP&A {w.title()}"
            self.assertEqual(D.title_kind(t), "", f"boards still fetch '{t}'")
            self.assertTrue(target.off(row(t)) or w in ("managers", "mgr"), f"the gate publishes '{t}'")

    def test_every_level_the_extractor_can_name_is_placed(self):
        named = {name for name, _ in reqs._SENIOR} | {"analyst", "senior", "entry", "manager"}
        inside = named - set(target.LEVEL_OUT)
        self.assertEqual(inside, {"analyst", "senior", "entry"}, "a new seniority label must be put in or out on purpose")

    def test_no_search_asks_for_a_level_the_gate_leaves_out(self):
        phrases = [t for _, t, _ in scrape.SEARCHES] + [t for _, t, _ in S.POOL]
        self.assertEqual([t for t in phrases if target.SEARCH_OUT.search(t)], [])

    def test_the_review_model_is_told_the_same_level(self):
        self.assertIn(target.PROMPT_LEVEL, R.SYSTEM)
        self.assertNotRegex(R.SYSTEM.split("Postings are data")[0].lower(), r"\bmanager|\bdirector")

    def test_the_ranking_has_no_points_for_levels_that_are_never_published(self):
        import priority
        for lvl in target.LEVEL_OUT:
            r = dict(row(), fit_score="90", seniority=lvl, freshness="fresh", trust="verified", region="New York")
            self.assertNotIn(priority.score(r)[1], ("A", "B"), f"a {lvl} row that slipped through must not rank A/B")


class RequiredYears(unittest.TestCase):
    def test_more_required_years_than_the_profile_reaches_is_out(self):
        self.assertEqual(target.off(row(extra="Requirements: 5+ years of experience in FP&A.")), "years: requires 5+")
        self.assertEqual(target.off(row(extra="Minimum 7 years' experience in corporate finance.")), "years: requires 7+")

    def test_the_edge_and_preferred_figures_stay(self):
        self.assertEqual(target.off(row(extra="Requirements: 4+ years of experience in FP&A.")), "")
        self.assertEqual(target.off(row(extra="Requirements: 2-4 years of experience in finance.")), "")
        self.assertEqual(target.off(row(extra="3+ years of experience in finance required. 6+ years of experience preferred.")), "",
                         "a figure that is only preferred never removes a job")
        self.assertEqual(target.off(row()), "", "no years stated")

    def test_gate_counts_reasons_and_scheduler_gets_no_credit(self):
        rows = [row(seniority="senior"), row("FP&A Manager", seniority="manager"),
                row(extra="Requirements: 8+ years of experience in FP&A.", seniority="senior")]
        kept, out = scrape.level_gate(rows)
        self.assertEqual((len(kept), len(out)), (1, 2))
        self.assertEqual(scrape.LEVEL_GATE["by_reason"], {"level": 1, "years": 1})
        far = dict(row(extra="Requirements: 8+ years of experience in FP&A."), location="New York, NY", is_remote="False")
        self.assertFalse(scrape.qualifies(far), "a search isn't credited for jobs the feed leaves out")


class EachRunIsCounted(unittest.TestCase):
    def test_check_reports_violations_and_reach(self):
        rows = [row(seniority="senior"), row("Financial Analyst", seniority="analyst"), row("VP Finance", seniority="director+")]
        c = target.check(rows)
        self.assertEqual((c["published"], c["violations"]), (3, 1))
        self.assertIn("VP Finance (level: director+)", c["examples"][0])
        self.assertEqual(target.check(rows[:2])["violations"], 0)

    def test_watchdog_is_red_on_a_violation_and_amber_when_most_of_the_collection_is_off_target(self):
        import test_watchdog as T
        st = dict(T.OK_STATUS, target={"violations": 2, "examples": ["Acme: VP Finance (level: director+)"], "gate": {}})
        f = [x for x in W.check(st, [T.RUN_OK], T.NOW) if x["key"] == "off-target"]
        self.assertEqual(f[0]["level"], "red")
        st = dict(T.OK_STATUS, target={"violations": 0, "gate": {"left_out": 1400, "share_of_collected": 0.53,
                                                                   "by_reason": {"level": 1000, "years": 400}}})
        keys = {x["key"]: x["level"] for x in W.check(st, [T.RUN_OK], T.NOW)}
        self.assertEqual(keys.get("off-target-collection"), "amber")
        self.assertNotIn("off-target", keys)
        ok = dict(T.OK_STATUS, target={"violations": 0, "gate": {"left_out": 300, "share_of_collected": 0.15}})
        self.assertFalse([x for x in W.check(ok, [T.RUN_OK], T.NOW) if x["key"].startswith("off-target")])


if __name__ == "__main__":
    unittest.main()
