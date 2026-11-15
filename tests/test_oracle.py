"""Oracle Recruiting Cloud adapter (Nov 14 2026), against the reply shapes shadow.py recorded in run #82."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ats  # noqa: E402
import discover as D  # noqa: E402
import identity  # noqa: E402
import scrape  # noqa: E402

HOST, SITE = "jpmc.fa.oraclecloud.com", "CX_1001"
LIST = {"items": [{"TotalJobsCount": 3, "requisitionList": [
    {"Id": "210807776", "Title": "Financial Planning & Analysis Analyst", "PrimaryLocation": "New York, NY, United States",
     "PostedDate": "2026-11-13", "secondaryLocations": [{"Name": "Jersey City, NJ, United States"}]},
    {"Id": "210802558", "Title": "Financial Analyst", "PrimaryLocation": "Bengaluru, Karnataka, India", "PostedDate": "2026-11-13"},
    {"Id": "210800001", "Title": "Software Engineer III", "PrimaryLocation": "New York, NY, United States", "PostedDate": "2026-11-12"}]}]}
DETAIL = {"items": [{"Id": "210807776", "ExternalDescriptionStr": "<p>Own the monthly <b>forecast</b> and annual budget.</p>",
                     "ExternalQualificationsStr": "<ul><li>3+ years of FP&amp;A experience</li></ul>"}]}


def fake(calls):
    def get(url, data=None, **kw):
        calls.append(url)
        if "recruitingCEJobRequisitionDetails" in url:
            return DETAIL
        if "FP%26A" in url or "keyword=\"FP&A\"" in url or calls.count(url) > 1:
            return {"items": [{"TotalJobsCount": 0, "requisitionList": []}]}
        return LIST
    return get


class Oracle(unittest.TestCase):
    def test_board_key_from_links(self):
        u = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210807776?utm_source=Indeed_Organic"
        self.assertEqual(ats.board_of(u), "oracle:jpmc.fa.oraclecloud.com|CX_1001")
        self.assertTrue(ats.REDIRECTS.match("https://JPMorganChase.contacthr.com/154185228"), "ContactHR links are opened")

    def test_reads_jobs_and_only_wanted_descriptions(self):
        calls = []
        jobs = ats.oracle(f"{HOST}|{SITE}", D.want, fake(calls))
        self.assertFalse(jobs.complete, "searches never close a job by its absence")
        ids = [j["id"] for j in jobs]
        self.assertEqual(len(set(ids)), 3, "each requisition once across searches")
        fpa = next(j for j in jobs if j["id"] == "oracle:jpmc:210807776")
        self.assertIn("forecast", fpa["description"])
        self.assertIn("3+ years", fpa["description"])
        self.assertIn("Jersey City", fpa["location"])
        self.assertEqual(fpa["url"], f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/210807776")
        self.assertEqual(sum(1 for c in calls if "Details" in c), 1, "no description fetched for India or engineering")

    def test_job_site_copy_folds_into_the_board_row(self):
        board_row = D.to_row(ats.oracle(f"{HOST}|{SITE}", D.want, fake([]))[0], "JPMorganChase", "2026-11-14")
        agg = {"id": "in-abc", "site": "indeed", "title": board_row["title"], "company": "JPMorgan Chase", "location": "New York, NY",
               "job_url": "https://www.indeed.com/viewjob?jk=abc",
               "job_url_direct": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210807776?utm_medium=jobboard"}
        self.assertTrue(identity.req_keys(agg) & identity.req_keys(board_row), "the same requisition key both ways")
        folded = scrape.annotate([agg], [board_row])
        self.assertEqual(folded, {"in-abc"})
        self.assertIn("in-abc", board_row["source_ids"], "the tracker keeps the lead it already had")

    def test_still_open_never_closes_on_an_empty_answer(self):
        u = f"https://{HOST}/hcmUI/CandidateExperience/en/sites/{SITE}/job/210807776"
        self.assertTrue(ats.still_open(u, lambda url, **kw: DETAIL))
        self.assertIsNone(ats.still_open(u, lambda url, **kw: {"items": []}))

    def test_scheduled_like_workday(self):
        self.assertTrue(scrape.NATIVE.match("oracle:jpmc:210807776"))
        self.assertTrue(D.due({"ok": "2026-11-13"}, "oracle:jpmc.fa.oraclecloud.com|CX_1001", "2026-11-14"))
        self.assertFalse(D.due({"ok": "2026-11-14"}, "oracle:jpmc.fa.oraclecloud.com|CX_1001", "2026-11-14"))


if __name__ == "__main__":
    unittest.main()
