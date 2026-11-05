"""Offline tests for the v3 direct sweep (ats.py, discover.py, scrape.main with direct=True). No network: fake APIs."""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ats  # noqa: E402
import discover as D  # noqa: E402
import scrape  # noqa: E402

scrape.random.uniform = lambda a, b: 0
scrape.time.sleep = lambda s: None
ats.time.sleep = lambda s: None
D.time.sleep = lambda s: None

GH_LIST = {"jobs": [
    {"id": 101, "title": "FP&A Analyst", "location": {"name": "New York, NY"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101", "first_published": "2026-10-30T10:00:00Z"},
    {"id": 102, "title": "Senior Accountant", "location": {"name": "New York, NY"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/102"},
    {"id": 103, "title": "Strategy & Operations Associate", "location": {"name": "Remote - US"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/103"},
    {"id": 104, "title": "Financial Analyst", "location": {"name": "London, UK"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/104"}]}
GH_DETAIL = {101: {"content": "&lt;p&gt;Build forecasts. Pay: $95,000 - $115,000&lt;/p&gt;"},
             103: {"content": "&lt;ul&gt;&lt;li&gt;Own the annual budget and monthly forecast&lt;/li&gt;&lt;li&gt;variance analysis vs plan, P&amp;amp;L review&lt;/li&gt;&lt;li&gt;long-range plan and scenario planning&lt;/li&gt;&lt;/ul&gt;"}}
LEVER = [{"id": "aaaa-1", "text": "Strategic Finance Analyst", "categories": {"location": "New York, NY", "allLocations": ["New York, NY"]}, "workplaceType": "hybrid",
          "hostedUrl": "https://jobs.lever.co/beta/aaaa-1", "createdAt": 1761900000000, "descriptionPlain": "Model the business.", "lists": [{"text": "Requirements", "content": "<li>Excel</li>"}],
          "salaryRange": {"min": 100000, "max": 130000, "interval": "per-year-salary"}}]
ASHBY = {"name": "Gamma", "jobs": [
    {"id": "g-1", "title": "Finance Associate", "location": "New York", "workplaceType": "Hybrid", "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/gamma/g-1", "publishedAt": "2026-10-31T00:00:00Z",
     "descriptionPlain": "Planning.", "compensation": {"compensationTiers": [{"components": [{"compensationType": "Salary", "minValue": 90000, "maxValue": 110000}]},
                                                                          {"components": [{"compensationType": "Salary", "minValue": 95000, "maxValue": 125000}]}]}},
    {"id": "g-2", "title": "FP&A Analyst", "location": "New York", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/gamma/g-2"}]}
WD_LIST = {"jobPostings": [{"title": "Financial Planning Analyst", "externalPath": "/job/New-York/Financial-Planning-Analyst_R12345", "locationsText": "New York, NY", "postedOn": "Posted 3 Days Ago"}]}
WD_DETAIL = {"jobPostingInfo": {"jobDescription": "<p>Forecasting</p>", "startDate": "2026-10-29", "location": "New York, NY"}}


class FakeAPI:
    def __init__(self, broken=()):
        self.calls, self.broken = [], set(broken)

    def __call__(self, url, data=None, method=None, tries=3, timeout=20, want_json=True, opener=None):
        self.calls.append(url)
        for b in self.broken:
            if b in url:
                raise ats.HttpError(500, url)
        if url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true":
            return GH_LIST
        if url == "https://boards-api.greenhouse.io/v1/boards/acme":
            return {"name": "Acme Corp"}
        if url.startswith("https://boards-api.greenhouse.io/v1/boards/acme/jobs/"):
            return GH_DETAIL.get(int(url.rsplit("/", 1)[1]), {"content": ""})
        if url == "https://api.lever.co/v0/postings/beta?mode=json":
            raise ats.HttpError(404, url)
        if url == "https://api.eu.lever.co/v0/postings/beta?mode=json":
            return LEVER
        if url.startswith("https://api.ashbyhq.com/posting-api/job-board/gamma"):
            return ASHBY
        if url.endswith("/wday/cxs/delta/Ext/jobs"):
            return WD_LIST
        if "/wday/cxs/delta/Ext/job/" in url:
            return WD_DETAIL
        raise ats.HttpError(404, url)


class Parsing(unittest.TestCase):
    def test_board_of(self):
        self.assertEqual(ats.board_of("https://job-boards.greenhouse.io/Acme/jobs/1?gh_src=x"), "greenhouse:acme")
        self.assertEqual(ats.board_of("https://boards.greenhouse.io/embed/job_board?for=acme"), "greenhouse:acme")
        self.assertEqual(ats.board_of("https://jobs.eu.lever.co/beta/aaaa-1/apply"), "lever:beta")
        self.assertEqual(ats.board_of("https://jobs.ashbyhq.com/Gamma%20Co/g-1"), "ashby:Gamma Co")
        self.assertEqual(ats.board_of("https://jobs.ashbyhq.com/api/x"), "")
        self.assertEqual(ats.board_of("https://jobs.smartrecruiters.com/BoschGroup/7443"), "smartrecruiters:BoschGroup")
        self.assertEqual(ats.board_of("https://gafsgi.wd5.myworkdayjobs.com/en-US/GAF_Careers/job/x_26622"), "workday:gafsgi|wd5|GAF_Careers")
        self.assertEqual(ats.board_of("https://example.com/careers"), "")

    def test_unwrap_and_text(self):
        u = "https://jsv3.recruitics.com/redirect?rx_cid=1&rx_url=https%3A%2F%2Fjob-boards.greenhouse.io%2Facme%2Fjobs%2F9"
        self.assertEqual(ats.unwrap(u), "https://job-boards.greenhouse.io/acme/jobs/9")
        self.assertEqual(ats.unwrap("https://grnh.se/abc", lambda x: "https://boards.greenhouse.io/acme/jobs/5"), "https://boards.greenhouse.io/acme/jobs/5")
        self.assertEqual(ats.unwrap("https://grnh.se/abc", lambda x: (_ for _ in ()).throw(OSError("x"))), "https://grnh.se/abc")
        self.assertEqual(ats.text_of("&lt;p&gt;P&amp;amp;L&lt;/p&gt;&lt;li&gt;One&lt;/li&gt;"), "P&L\n- One")
        self.assertEqual(ats.wd_posted("Posted 3 Days Ago", datetime(2026, 11, 1).date()), "2026-10-29")
        self.assertEqual(ats.wd_posted("Posted Today", datetime(2026, 11, 1).date()), "2026-11-01")
        self.assertEqual(ats.wd_posted("Posted 30+ Days Ago", datetime(2026, 11, 1).date()), "2026-10-02")

    def test_filters(self):
        j = lambda t, loc="New York, NY", d="", r=False: {"title": t, "location": loc, "description": d, "remote": r, "company": "Acme"}
        self.assertTrue(D.keep(j("FP&A Analyst")))
        self.assertFalse(D.keep(j("Senior Accountant")))
        self.assertFalse(D.keep(j("Account Executive, Financial Services")), "a sales seat with 'financial' in the name")
        self.assertTrue(D.keep(j("Sales Finance Analyst, FP&A")), "a strong title wins over a sales word")
        self.assertFalse(D.keep(j("Financial Analyst", "London, UK")))
        self.assertTrue(D.keep(j("Financial Analyst", "Remote - US")))
        self.assertFalse(D.keep(j("Strategy & Operations Associate", d="Run the warehouse schedule.")))
        x = j("Strategy & Operations Associate", d="annual budget, monthly forecast, variance analysis vs plan, P&L review, long-range plan")
        self.assertTrue(D.keep(x) and x.get("unconventional"))
        real, D.NEVER = D.NEVER, D._Excluded(phrases=["acme cable"])
        try:
            self.assertFalse(D.keep(j("Financial Analyst", d="", r=False) | {"company": "Acme Cable Inc"}))
        finally:
            D.NEVER = real
        self.assertTrue(D._Excluded(phrases=["acme cable"]).search("Acme  Cable, Inc."), "matched on words, any spacing or case")
        self.assertFalse(D._Excluded(phrases=["acme cable"]).search("Acme Cabletown"))
        self.assertTrue(D._Excluded(phrases=[], extra="zz co").search("ZZ Co"), "FEED_EXCLUDE adds a regex")
        self.assertEqual(len(D.NEVER.h), 19, "the real list is loaded")


class Boards(unittest.TestCase):
    def test_each_ats_parses_with_its_gotchas(self):
        api = FakeAPI()
        gh = ats.fetch_board("greenhouse:acme", D.want, api)
        self.assertEqual(gh[0]["id"], "greenhouse:101")
        self.assertEqual(gh[0]["company"], "Acme Corp")
        self.assertIn("Build forecasts", gh[0]["description"])
        self.assertEqual((gh[0]["pay_min"], gh[0]["pay_max"]), ("95000", "115000"))
        self.assertFalse(any("jobs/102" in c or "jobs/104" in c for c in api.calls), "no detail call for jobs the feed won't keep")
        lv = ats.fetch_board("lever:beta", D.want, api)
        self.assertEqual(lv[0]["id"], "lever:aaaa-1")
        self.assertEqual(lv[0]["interval"], "yearly")
        self.assertIn("Requirements\n- Excel", lv[0]["description"])
        ab = ats.fetch_board("ashby:gamma", D.want, api)
        self.assertEqual([x["id"] for x in ab], ["ashby:g-1"], "unlisted jobs skipped")
        self.assertFalse(ab[0]["remote"], "isRemote=true on a hybrid job is not remote")
        self.assertEqual((ab[0]["pay_min"], ab[0]["pay_max"]), (90000, 125000), "min of mins, max of maxes across tiers")
        wd = ats.fetch_board("workday:delta|wd1|Ext", D.want, api)
        self.assertEqual(wd[0]["id"], "workday:R12345", "same key the finder uses for Workday")
        self.assertEqual(wd[0]["posted"], "2026-10-29")
        self.assertEqual(wd[0]["url"], "https://delta.wd1.myworkdayjobs.com/Ext/job/New-York/Financial-Planning-Analyst_R12345")


def fakejobspy(rows):
    class DF:
        def to_dict(self, o):
            return rows

    def scrape_jobs(**kw):
        return DF() if kw["site_name"][0] == "indeed" and kw.get("search_term") == "FP&A analyst" else None
    return scrape_jobs


def agg(i, title, company, direct=""):
    return {"id": f"in-{i}", "site": "indeed", "title": title, "company": company, "location": "New York, NY", "is_remote": False, "date_posted": "2026-10-31",
            "job_url": f"https://www.indeed.com/viewjob?jk={i}", "job_url_direct": direct, "min_amount": "", "max_amount": "", "interval": "", "description": "x"}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "jobs.csv")
        self.st = os.path.join(self.d, "feed_status.json")
        with open(os.path.join(self.d, "boards.json"), "w") as f:
            json.dump({"boards": {"greenhouse:acme": {"company": "Acme Corp", "src": "seed"}, "ashby:gamma": {"company": "Gamma", "src": "seed"}}}, f)
        open(os.path.join(self.d, "seeds.txt"), "w").write("Zeta Health\n")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_once(self, api, rows, when="2026-11-01T12:00:00+00:00"):
        scrape.SEARCHES[:] = [("indeed", "FP&A analyst", 10)]
        return scrape.main(fakejobspy(rows), self.out, self.st, now=datetime.fromisoformat(when), direct=True, get=api,
                           resolve=lambda u: "https://jobs.eu.lever.co/beta/aaaa-1", cc_text=lambda u: (_ for _ in ()).throw(OSError("cc down")), weekday=2)

    def read(self):
        csv.field_size_limit(10 ** 8)
        return list(csv.DictReader(open(self.out, encoding="utf-8")))

    def test_direct_rows_keys_links_metrics(self):
        api = FakeAPI()
        s = self.run_once(api, [agg(1, "FP&A Analyst", "Acme Corp."), agg(2, "Strategic Finance Analyst", "Beta", "https://grnh.se/xyz")])
        rows = {r["id"]: r for r in self.read()}
        self.assertIn("greenhouse:101", rows)
        self.assertIn("greenhouse:103", rows, "unconventional title kept from its description")
        self.assertNotIn("greenhouse:102", rows)
        self.assertIn("ashby:g-1", rows)
        self.assertIn("lever:aaaa-1", rows, "a short link in an Indeed row led to a new board (Lever, EU host)")
        # v7: the Indeed copy of a board job is folded into the board row (one row per opening, CONTRACT.md invariant 1)
        self.assertNotIn("in-1", rows)
        self.assertIn("in-1", rows["greenhouse:101"]["source_ids"].split(";"))
        self.assertIn("indeed", rows["greenhouse:101"]["also_on"])
        self.assertGreaterEqual(s["direct"]["aggregator_rows_folded"], 1)
        d = s["direct"]
        self.assertGreaterEqual(d["boards_new"], 1)
        self.assertEqual(d["boards_failed"], 0)
        self.assertGreaterEqual(d["rows_direct"], 4)
        self.assertGreater(d["pct_direct"], 50)
        self.assertEqual(d.get("unconventional"), 1)
        self.assertIn("harvest_error", d, "Common Crawl down: noted, run goes on")
        db = json.load(open(os.path.join(self.d, "boards.json")))
        self.assertEqual(db["boards"]["lever:beta"]["src"], "feed-link")
        self.assertEqual(db["neg"].get("zeta health"), "2026-11-01", "a seed with no board is remembered for 2 weeks")

    def test_closed_jobs_leave_but_unread_boards_keep_theirs(self):
        self.run_once(FakeAPI(), [])
        saved = GH_LIST["jobs"][:]
        GH_LIST["jobs"] = [j for j in saved if j["id"] != 101]
        try:
            self.run_once(FakeAPI(broken=["api.ashbyhq.com"]), [], "2026-11-01T16:00:00+00:00")
            ids = {r["id"] for r in self.read()}
            self.assertNotIn("greenhouse:101", ids, "left a board that was read fine: closed")
            self.assertIn("ashby:g-1", ids, "board couldn't be read: its jobs are kept")
            st = json.load(open(self.st))
            self.assertEqual(st["direct"]["boards_failed"], 1)
            self.assertEqual(st["direct"]["kept_unread"], 1)
        finally:
            GH_LIST["jobs"] = saved

    def test_direct_part_never_breaks_the_feed(self):
        orig = scrape.direct_step
        scrape.direct_step = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            s = self.run_once(FakeAPI(), [agg(1, "FP&A Analyst", "Acme")])
            self.assertIn("boom", s["direct"]["error"])
            self.assertTrue(any(r["id"] == "in-1" for r in self.read()))
        finally:
            scrape.direct_step = orig

    def test_failing_boards_rest(self):
        info = {"fails": 5, "ok": ""}
        days = [D.due(info, "greenhouse:x", f"2026-11-0{d}") for d in range(1, 8)]
        self.assertEqual(sum(days), 1, "a board failing 5 runs is tried once a week")
        self.assertTrue(D.due({"fails": 0}, "greenhouse:x", "2026-11-01"))
        self.assertFalse(D.due({"ok": "2026-11-01"}, "workday:a|wd1|b", "2026-11-01"), "Workday once a day")


if __name__ == "__main__":
    unittest.main()
