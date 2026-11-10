"""Offline tests for contract v8 (Nov 9 2026): level realism, who posted the job, and the late duplicate fold.

The cases come from the Nov 9 feed: 83 manager-titled jobs ranked A, recruiter postings (one for an unnamed client at A) that
trust could only call verified, and 18 aggregator copies of board jobs left as separate rows.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import poster  # noqa: E402
import priority  # noqa: E402
import scrape  # noqa: E402

DESC = "Build the forecast and annual budget; variance analysis; financial models for leadership."


def job(**k):
    base = dict(id="greenhouse:1", title="Financial Analyst, FP&A", company="Acme", description=DESC, fit_score="92",
                freshness="fresh", ghost_risk="low", trust="verified", pay_high="120000", region="New York City",
                years_min="2", seniority="analyst", status="new", poster="employer")
    base.update(k)
    return base


class Level(unittest.TestCase):
    def tier(self, **k):
        return priority.assess(job(**k))

    def test_in_range_analyst_still_tops_the_list(self):
        r = self.tier()
        self.assertEqual((r["priority"], r["level_fit"]), ("A", "in_range"))
        self.assertEqual(self.tier(title="Senior Financial Analyst", seniority="senior", years_min="3")["level_fit"], "in_range")

    def test_manager_seat_is_a_stretch_and_never_A(self):
        r = self.tier(title="Manager, FP&A", seniority="manager", years_min="")
        self.assertEqual(r["level_fit"], "stretch")
        self.assertEqual(r["priority"], "B", "a perfect-fit manager seat no longer ranks with in-range seats")
        self.assertIn("manager title", r["priority_why"])
        self.assertIn("a stretch", r["priority_why"])

    def test_out_of_range_is_capped_at_C(self):
        for title, lvl, yrs in (("Senior Manager, FP&A", "manager", "3"), ("Manager, Finance", "manager", "4-6"),
                                ("Principal Financial Analyst", "senior", ""), ("Associate Director, FP&A", "director+", ""), ("Senior Finance Manager", "manager", ""),
                                ("AVP, Corporate Finance", "analyst", "3-5"), ("Assistant Controller", "analyst", "")):
            r = self.tier(title=title, seniority=lvl, years_min=yrs)
            self.assertEqual(r["level_fit"], "out_of_range", title)
            self.assertIn(r["priority"], ("C", "D"), title)

    def test_four_years_is_a_stretch_not_a_wall(self):
        r = self.tier(title="Financial Analyst", years_min="4")
        self.assertEqual((r["level_fit"], r["priority"]), ("stretch", "B"))

    def test_staff_accountant_is_not_a_staff_level_title(self):
        self.assertNotEqual(priority.level_fit(job(title="Staff Accountant"), 0)[0], "out_of_range")
        self.assertEqual(priority.level_fit(job(title="Staff Financial Analyst", seniority="senior"), 0)[0], "out_of_range")

    def test_level_never_raises_a_job(self):
        weak = self.tier(fit_score="40")
        self.assertEqual(weak["priority"], "C")
        self.assertEqual(self.tier(fit_score="40", seniority="entry")["priority"], "C")


class Poster(unittest.TestCase):
    def one(self, **k):
        rows = [job(**k)]
        poster.assess_all(rows)
        return rows[0]

    def test_staffing_firm_by_name(self):
        for co in ("Robert Half", "Kforce Inc", "Vaco by Highspring", "The Greene Group", "Lumera Search Partners",
                   "Acme Staffing LLC", "Northbound Executive Search"):
            self.assertEqual(self.one(company=co)["poster"], "recruiter", co)

    def test_lookalike_names_are_employers(self):
        for co in ("CFS Brands, LLC", "Boston Consulting Group", "The Brattle Group", "Halfmoon Capital", "Staffbase",
                   "Hayes Partners"):
            self.assertEqual(self.one(company=co)["poster"], "employer", co)

    def test_client_wording_marks_a_recruiter_posting(self):
        r = self.one(company="Nextiya", description="Our client, a privately held environmental services company, is seeking "
                                                      "an FP&A Analyst to build the budget.")
        self.assertEqual(r["poster"], "recruiter_unnamed")
        self.assertIn("not named", r["poster_why"])
        r = self.one(company="Coda", description="We are partnering with a growing fintech to find a Senior Financial Analyst.")
        self.assertEqual(r["poster"], "recruiter_unnamed")

    def test_advisory_firms_talking_about_their_clients_are_not_recruiters(self):
        for d in ("We advise our clients on transactions and act on behalf of our clients in disputes.",
                  "Protect confidential client information and data at all times.",
                  "Serve our clients with forecasts; our clients are seeking better planning."):
            self.assertEqual(self.one(company="Alvarez & Marsal", description=d)["poster"], "employer", d)

    def test_employer_judged_across_its_postings(self):
        client = "Our client is seeking a Financial Analyst to own the forecast."
        rows = [job(id=f"li-{i}", company="Job Mobz", description=d) for i, d in enumerate([client, client, DESC])]
        rows.append(job(id="li-9", company="Morgan Stanley", description=client))
        rows += [job(id=f"li-1{i}", company="Morgan Stanley", description=DESC) for i in range(4)]
        poster.assess_all(rows)
        self.assertEqual(rows[2]["poster"], "recruiter", "2 of 3 postings for a client: the third is a recruiter posting too")
        self.assertIn("2 of 3", rows[2]["poster_why"])
        self.assertEqual(rows[4]["poster"], "employer", "one client sentence doesn't make a bank a recruiter elsewhere")

    def test_contract_seats_are_named(self):
        r = self.one(company="Robert Half", description=DESC + " This is a 6-month contract-to-hire role.")
        self.assertIn("contract", r["poster_why"])

    def test_effect_on_priority_separate_from_trust(self):
        r = priority.assess(job(poster="recruiter_unnamed"))
        self.assertEqual(r["priority"], "C", "unnamed employer: can't be checked against companies to avoid")
        self.assertEqual(r["trust"], "verified", "trust is untouched")
        r = priority.assess(job(poster="recruiter"))
        self.assertIn(r["priority"], ("A", "B"), "a named recruiter is never pushed out for being a recruiter")
        self.assertIn("recruiter", r["priority_why"])


if __name__ == "__main__":
    unittest.main()
