"""Regression tests for the Nov 3 2026 opportunity audit (four places where existing logic lost information or decided wrong):

  1. merge():  a job re-observed from the same source with a changed description, pay or title kept the old row when both
               copies had a direct link and a description (a quality tie), so the feed showed stale facts as freshly confirmed.
  2. priority: the years figure is the most demanding one in the posting (the gate's rule, kept), so "2 years required,
               5 preferred" was penalized and capped at C as if 5 were mandatory.
  3. guard():  the sudden-drop guard only covered boards whose usual count is 10+, so one successful-but-empty read of a small
               board closed every job on it, silently (closed jobs are "explained", so the audit never listed them).
  4. reqs:     "Lead" in a title meant manager level; Finance Lead / Lead Analyst are usually individual contributors.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ats  # noqa: E402
import discover as D  # noqa: E402
import history  # noqa: E402
import priority  # noqa: E402
import reqs  # noqa: E402
import scrape  # noqa: E402

ats.time.sleep = lambda s: None
D.time.sleep = lambda s: None
scrape.time.sleep = lambda s: None

LINK = "https://boards.greenhouse.io/acme/jobs/1"


def row(**k):
    base = {c: "" for c in scrape.COLS}
    base.update(id="greenhouse:1", site="greenhouse", title="FP&A Analyst", company="Acme", location="New York, NY", is_remote="False",
                date_posted="2026-10-20", job_url=LINK, job_url_direct=LINK, min_amount="90000", max_amount="110000", interval="yearly",
                description="Build the annual forecast.", first_seen="2026-10-20", last_seen="2026-10-30", source_ids="greenhouse:1")
    base.update(k)
    return base


def fresh(**k):
    """What a board read produces for the same job (no lifecycle columns yet)."""
    r = {c: row()[c] for c in ("id", "site", "title", "company", "location", "is_remote", "date_posted", "job_url", "job_url_direct",
                               "min_amount", "max_amount", "interval", "description")}
    r.update(k)
    return r


class MergeKeepsUpdates(unittest.TestCase):
    def test_reobserved_job_carries_new_description_pay_and_title(self):
        old = row()
        new = fresh(description="Build the annual forecast and own headcount planning.", min_amount="95000", max_amount="115000",
                    title="Senior FP&A Analyst", date_posted="2026-11-01")
        out = scrape.merge([old], [new], "2026-11-01")
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual((r["id"], r["first_seen"], r["last_seen"]), ("greenhouse:1", "2026-10-20", "2026-11-01"), "identity and lifecycle kept")
        self.assertEqual(r["description"], new["description"], "the employer's edit survives a quality tie")
        self.assertEqual((r["min_amount"], r["max_amount"]), ("95000", "115000"))
        self.assertEqual(r["title"], "Senior FP&A Analyst")
        self.assertEqual(r["date_posted"], "2026-11-01")
        self.assertEqual(r["job_url_direct"], LINK)

    def test_history_records_the_change(self):
        # merge() handles aggregator rows (company-board rows are rebuilt from each read), so an aggregator id; for those the
        # history signature leaves the description out by design (each site renders it differently), pay and title are in.
        old = row(id="in-abc", site="indeed", source_ids="in-abc", job_url="https://www.indeed.com/viewjob?jk=abc")
        h = {"v": 1, "jobs": {}}
        history.update(h, [old], "2026-10-30")
        out = scrape.merge([old], [fresh(id="in-abc", site="indeed", job_url=old["job_url"], description="Now with headcount planning.",
                                         min_amount="95000", max_amount="115000")], "2026-11-01")
        history.update(h, out, "2026-11-01")
        self.assertEqual(out[0]["description"], "Now with headcount planning.")
        self.assertEqual(out[0]["status"], "changed")
        self.assertIn("pay: 90000-110000 yearly -> 95000-115000 yearly", out[0]["changes"])

    def test_two_sites_copies_do_not_take_turns_overwriting(self):
        old = row(id="in-abc", site="indeed", source_ids="in-abc;li-123", also_on="linkedin", job_url="https://www.indeed.com/viewjob?jk=abc")
        li = fresh(id="li-123", site="linkedin", job_url="https://www.linkedin.com/jobs/view/123456", title="FP&A Analyst (Hybrid)",
                   description="LinkedIn rendering.")
        indeed = fresh(id="in-abc", site="indeed", job_url=old["job_url"])
        for order in ((li, indeed), (indeed, li)):
            out = scrape.merge([old], list(order), "2026-11-01")
            self.assertEqual((out[0]["title"], out[0]["description"]), (old["title"], old["description"]), "the row's own copy is the source of truth")

    def test_a_thinner_fresh_copy_never_erases_what_was_known(self):
        old = row()
        out = scrape.merge([old], [fresh(description="", min_amount="", max_amount="", interval="", job_url_direct="")], "2026-11-01")
        r = out[0]
        self.assertEqual(r["description"], old["description"])
        self.assertEqual((r["min_amount"], r["max_amount"], r["interval"]), ("90000", "110000", "yearly"))
        self.assertEqual(r["job_url_direct"], LINK, "the best application link is kept")
        self.assertEqual(r["last_seen"], "2026-11-01")

    def test_new_link_from_the_same_source_wins(self):
        out = scrape.merge([row()], [fresh(job_url_direct="https://job-boards.greenhouse.io/acme/jobs/1?gh_src=x")], "2026-11-01")
        self.assertEqual(out[0]["job_url_direct"], "https://job-boards.greenhouse.io/acme/jobs/1?gh_src=x")

    def test_another_sites_copy_does_not_overwrite_the_boards_text(self):
        # An aggregator copy of a company-board job carries its own rendering of the text: not an edit by the employer.
        old = row()
        agg = fresh(id="in-abc", site="indeed", job_url="https://www.indeed.com/viewjob?jk=abc", job_url_direct=LINK,
                    description="Build the annual forecast. (Indeed rendering)", title="FP&A Analyst - Hybrid")
        out = scrape.merge([old], [agg], "2026-11-01")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["description"], old["description"])
        self.assertEqual(out[0]["title"], old["title"])
        self.assertEqual(out[0]["source_ids"], "greenhouse:1;in-abc")
        self.assertEqual(out[0]["also_on"], "indeed")

    def test_better_copy_still_wins_as_before(self):
        old = row(job_url_direct="", description="")                      # the stored copy had no link and no text
        out = scrape.merge([old], [fresh(id="li-9", site="linkedin", job_url="https://www.linkedin.com/jobs/view/123456",
                                         description="Full text from LinkedIn.")], "2026-11-01")
        self.assertEqual(out[0]["description"], "Full text from LinkedIn.")
        self.assertEqual(out[0]["job_url_direct"], LINK)


class RequiredVersusPreferredYears(unittest.TestCase):
    REQ5 = "Requirements:\n- 5+ years of FP&A experience\n- Bachelor's degree in Finance"
    REQ2_PREF5 = "Basic qualifications:\n- 2+ years of experience in corporate finance\nPreferred qualifications:\n- 5+ years of FP&A experience\n- MBA"
    INLINE = "2+ years of relevant experience required; 5+ years of FP&A experience preferred."
    PREF5 = "5+ years of experience in financial planning is preferred but not required."
    RANGE = "3-5 years of relevant experience."

    def test_extractor_separates_required_from_preferred(self):
        self.assertEqual(reqs.years_required(self.REQ5), (5, 5))
        self.assertEqual(reqs.years_required(self.REQ2_PREF5), (2, 2), "the preferred section's figure is not required")
        self.assertEqual(reqs.years_required(self.INLINE), (2, 2))
        self.assertEqual(reqs.years_required(self.PREF5), (0, 0), "nothing required")
        self.assertEqual(reqs.years_required(self.RANGE), (3, 5))
        self.assertEqual(reqs.years_required("Ideally you bring 7+ years of experience. Must have 4+ years of financial modeling experience."), (4, 4))
        # from the Nov 3 feed: the soft word qualifies the setting, not the years
        self.assertEqual(reqs.years_required("* 5+ years of progressive experience in FP&A, preferably in the insurance industry"), (5, 5))
        self.assertEqual(reqs.years_required("* 12+ years of experience in Strategic Finance, ideally within high-growth tech"), (12, 12))
        self.assertEqual(reqs.years_required("* ideally over 7 years of experience in quantitative analysis, preferably in asset management"), (0, 0))
        self.assertEqual(reqs.years_required("**Preferred qualifications**\n* 7+ years of experience\n\nBenefits:\n* 401k\n\n5+ years of experience required"), (5, 5))
        self.assertEqual(reqs.years_required("Preferred Qualifications\n* MBA\n* 7+ years of experience\n\nCompensation:\n$120,000"), (0, 0))

    def test_years_min_column_keeps_the_gates_rule(self):
        self.assertEqual(reqs.requirements("Analyst", self.REQ2_PREF5)["years_min"], "5", "contract v7: the most demanding figure")
        self.assertEqual(reqs.requirements("Analyst", self.RANGE)["years_min"], "3-5")

    def test_priority_penalizes_and_caps_on_required_years_only(self):
        good = row(fit_score="78", freshness="fresh", ghost_risk="low", trust="verified", pay_high="120000", region="New York City",
                   seniority="analyst", status="new")

        def tier(desc):
            r = dict(good, description=desc.replace("\n", "\\n"), years_min=reqs.requirements("Analyst", desc)["years_min"])
            words = "; ".join(w for _, w in priority.score(r)[2])
            return priority.assess(r)["priority"], words

        self.assertEqual(tier(self.REQ5)[0], "C", "5 required: capped, the gate decides")
        t, words = tier(self.REQ2_PREF5)
        self.assertEqual(t, "A", "2 required, 5 preferred: not capped")
        self.assertIn("asks for 2+ years (5+ preferred)", words, "the softer signal is still shown")
        self.assertEqual(tier(self.INLINE)[0], "A")
        t, words = tier(self.PREF5)
        self.assertNotEqual(t, "C", "preferred only: a soft penalty, never the cap")
        self.assertIn("prefers 5+ years", words)
        self.assertEqual(tier(self.RANGE)[0], "A")
        self.assertEqual(tier("7+ years of experience required.")[0], "C")

    def test_without_a_description_the_column_still_decides(self):
        good = row(fit_score="78", freshness="fresh", ghost_risk="low", trust="verified", pay_high="120000", region="New York City",
                   seniority="analyst", status="new", description="")
        self.assertEqual(priority.assess(dict(good, years_min="5-7"))["priority"], "C")
        self.assertEqual(priority.assess(dict(good, years_min="2-4"))["priority"], "A")


class EmptyReadOfASmallBoard(unittest.TestCase):
    @staticmethod
    def jobs(n, complete=True):
        j = ats.Jobs({"id": f"greenhouse:{i}", "title": "x", "location": "NY", "url": "u"} for i in range(n))
        j.complete = complete
        return j

    def test_one_empty_read_is_suspect_until_it_repeats(self):
        for base in (1, 5, 9, 20):
            info = {"hist": [base, base, base]}
            out, complete, note = D.guard(info, self.jobs(0))
            self.assertFalse(complete, f"usual {base}: one empty read closes nothing")
            self.assertIn("sudden drop", note)
            out, complete, note = D.guard(info, self.jobs(0))
            self.assertFalse(complete, f"usual {base}: a second empty read still waits")
            out, complete, note = D.guard(info, self.jobs(0))
            self.assertTrue(complete, f"usual {base}: three empty reads in a row and the board really is empty")
            self.assertEqual(info["hist"], [0])
            out, complete, note = D.guard(info, self.jobs(0))
            self.assertTrue(complete, "a board that stays empty is not re-flagged")

    def test_small_boards_can_still_shrink_and_a_read_in_between_resets(self):
        self.assertTrue(D.guard({"hist": [5, 5, 5]}, self.jobs(1))[1], "a real shrink on a small board")
        self.assertTrue(D.guard({"hist": [0, 0, 0]}, self.jobs(0))[1], "usually empty")
        self.assertTrue(D.guard({}, self.jobs(0))[1], "never read before")
        self.assertTrue(D.guard({"hist": [3]}, self.jobs(2))[1])
        info = {"hist": [4, 4, 4]}
        D.guard(info, self.jobs(0))
        D.guard(info, self.jobs(4))
        self.assertEqual(info["drop"], 0, "a normal read in between resets the count")

    def test_empty_read_does_not_close_the_boards_jobs(self):
        d = tempfile.mkdtemp()
        try:
            boards = {"greenhouse:b0": {"company": "B0", "src": "cc", "hist": [3, 3, 3]}}
            json.dump({"boards": boards}, open(os.path.join(d, "boards.json"), "w"))
            prev = [row(id="greenhouse:900", title="FP&A Analyst", company="B0", job_url="https://boards.greenhouse.io/b0/jobs/900",
                        job_url_direct="https://boards.greenhouse.io/b0/jobs/900", last_seen="2026-10-31")]

            def api(url, data=None, **k):
                if "/boards/b0" in url and url.endswith("content=true"):
                    return {"jobs": []}
                raise ats.HttpError(404, url)
            out, held, stats, closed = scrape.direct_step([], prev, "2026-11-01", os.path.join(d, "boards.json"), api, lambda u: u,
                                                          lambda u: (_ for _ in ()).throw(OSError("offline")))
            self.assertEqual(closed, [], "one empty read of a 3-job board closes nothing")
            self.assertEqual([r["id"] for r in out], ["greenhouse:900"], "last good snapshot kept")
            self.assertEqual(stats["drops"], 1)
        finally:
            shutil.rmtree(d)


class LeadIsNotAManagerByTitle(unittest.TestCase):
    IC = "3+ years of FP&A experience. Build models, partner with department heads, present to leadership."
    MGR = "5+ years of experience. Manage a team of three analysts, including hiring and performance reviews."

    def test_lead_titles_need_management_evidence(self):
        self.assertEqual(reqs.requirements("Finance Lead", self.IC)["seniority"], "analyst")
        self.assertEqual(reqs.requirements("Lead Analyst, FP&A", self.IC)["seniority"], "analyst")
        self.assertEqual(reqs.requirements("Lead Analyst, FP&A", "6+ years of FP&A experience.")["seniority"], "senior")
        self.assertEqual(reqs.requirements("Finance Lead", self.MGR)["seniority"], "manager")
        self.assertEqual(reqs.requirements("FP&A Lead", "You will have 2 direct reports.")["seniority"], "manager")
        self.assertEqual(reqs.requirements("Team Lead, Finance", self.IC)["seniority"], "manager", "a team lead leads people")
        self.assertEqual(reqs.requirements("Senior Finance Manager", self.IC)["seniority"], "manager")
        self.assertEqual(reqs.requirements("Finance Manager", "")["seniority"], "manager")
        self.assertEqual(reqs.requirements("Senior Financial Analyst", self.MGR)["seniority"], "senior", "evidence only reclassifies leads")


if __name__ == "__main__":
    unittest.main()
