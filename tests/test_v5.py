"""Offline tests for v5: Common Crawl index files and the board registry, new application systems, the request manager,
job history, requirement extraction."""
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ats  # noqa: E402
import ccindex  # noqa: E402
import discover as D  # noqa: E402
import harvest  # noqa: E402
import history  # noqa: E402
import reqs  # noqa: E402
import scrape  # noqa: E402

ats.time.sleep = lambda s: None
D.time.sleep = lambda s: None


class FakeIndex:
    """A crawl's cluster.idx and cdx blocks, served by byte range."""

    def __init__(self, records_by_prefix, filler=4000):
        lines, blocks = [], {}
        keys = [f"aa,filler{i:05d})/" for i in range(filler)]                       # before everything
        recs = sorted((k, u) for k, u in records_by_prefix)
        keys += [k for k, _ in recs] + [f"zz,filler{i:05d})/" for i in range(filler)]
        data, off = b"", 0
        allrecs = [(k, f"https://example.com/{i}") for i, k in enumerate(keys)]
        url_of = dict(recs)
        allrecs = [(k, url_of.get(k, u)) for k, u in allrecs]
        allrecs.sort()
        for i in range(0, len(allrecs), 3):                                            # three records a block
            chunk = allrecs[i:i + 3]
            body = "".join(f"{k} 20261001000000 {json.dumps({'url': u})}\n" for k, u in chunk).encode()
            gz = gzip.compress(body)
            lines.append(f"{chunk[0][0]} 20261001000000\tcdx-00001.gz\t{off}\t{len(gz)}\t{i // 3}")
            data += gz
            off += len(gz)
        self.idx = ("\n".join(lines) + "\n").encode()
        self.data = data
        self.calls = 0

    def size(self, url):
        return len(self.idx)

    def __call__(self, url, start, end):
        return self.get(url, start, end)

    def get(self, url, start, end):
        self.calls += 1
        src = self.idx if url.endswith("cluster.idx") else self.data
        return src[start:end + 1]


class CommonCrawl(unittest.TestCase):
    RECS = [("io,greenhouse,job-boards)/acme/jobs/1", "https://job-boards.greenhouse.io/acme/jobs/1"),
            ("io,greenhouse,job-boards)/acme/jobs/2", "https://job-boards.greenhouse.io/acme/jobs/2"),
            ("io,greenhouse,job-boards)/beta/jobs/9", "https://job-boards.greenhouse.io/beta/jobs/9"),
            ("com,ashbyhq,jobs)/OpenCo/abc", "https://jobs.ashbyhq.com/OpenCo/abc"),
            ("com,myworkdayjobs,wd5,gamma)/en-us/External/job/NY/FPA_R1", "https://gamma.wd5.myworkdayjobs.com/en-US/External/job/NY/FPA_R1"),
            ("hr,breezy,delta)/p/123-analyst", "https://delta.breezy.hr/p/123-analyst")]

    def test_binary_search_finds_only_the_prefix_blocks(self):
        fx = FakeIndex(self.RECS)
        lines, nxt = ccindex.find_blocks("CC-X", "io,greenhouse,job-boards)/", get=fx.get, size=len(fx.idx))
        self.assertIsNone(nxt)
        boards = {ats.board_of(u) for b in lines for _, u in ccindex.read_block("CC-X", b, get=fx.get)}
        self.assertTrue({"greenhouse:acme", "greenhouse:beta"} <= boards)
        self.assertLess(len(lines), 6, "a handful of blocks, not the whole index")
        self.assertLess(fx.calls, 60)

    def test_registry_harvests_verifies_and_resumes(self):
        d = tempfile.mkdtemp()
        try:
            fx = FakeIndex(self.RECS)
            db = {"boards": {"greenhouse:beta": {"company": "Beta", "added": "2026-10-30"}}, "cc": {"queue": [["lever:old", 3]], "rejected": {"lever:gone": "2026-10-20"}}}

            def api(url, data=None, **k):
                if "greenhouse.io/v1/boards/acme/jobs" in url:
                    return {"jobs": [{"id": 1, "title": "FP&A Analyst", "location": {"name": "Yonkers, NY"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1"}]}
                if "greenhouse.io/v1/boards/acme" in url:
                    return {"name": "Acme"}
                if "ashbyhq.com" in url:
                    return {"name": "OpenCo", "jobs": [{"id": "x", "title": "Engineer", "location": "London", "workplaceType": "OnSite"}]}
                if "breezy.hr" in url:
                    return []
                if "myworkdayjobs" in url:
                    return {"jobPostings": [{"title": "Analyst", "locationsText": "New York, NY", "externalPath": "/job/x_R1"}], "total": 1}
                raise ats.HttpError(404, url)
            stats = {"boards_new": 0}
            harvest.HARVEST_BLOCKS = 1                                  # stop part-way: the checkpoint carries on next run
            harvest.step(db, d, "2026-11-01", stats, D.place_ok, D.title_kind, D.add_board, api, fx, lambda: ["CC-X"])
            st = json.load(open(os.path.join(d, "harvest_state.json")))
            self.assertIsNotNone(st["cursor"], "a checkpoint was saved mid-prefix")
            harvest.HARVEST_BLOCKS = 500
            for day in ("2026-11-02", "2026-11-03"):
                harvest.step(db, d, day, stats, D.place_ok, D.title_kind, D.add_board, api, fx, lambda: ["CC-X"])
            reg = harvest.load(os.path.join(d, "registry.tsv"))
            self.assertEqual(reg["greenhouse:acme"]["status"], "local")
            self.assertIn("greenhouse:acme", db["boards"], "a board with a Yonkers job joins the boards that are read")
            self.assertEqual(db["boards"]["greenhouse:acme"]["src"], "registry")
            self.assertEqual(reg["ashby:OpenCo"]["status"], "other")
            self.assertEqual(reg["breezy:delta"]["status"], "empty")
            self.assertEqual(reg["workday:gamma|wd5|External"]["status"], "local")
            self.assertEqual(reg["greenhouse:beta"]["status"], "local", "boards already read carry over")
            self.assertEqual(reg["lever:old"]["status"], "gone", "the v4 queue was migrated and checked")
            self.assertEqual(reg["lever:gone"]["status"], "other")
            self.assertNotIn("cc", db)
            self.assertFalse(harvest.due_for_check(reg["ashby:OpenCo"], "2026-11-20"))
            self.assertTrue(harvest.due_for_check(reg["ashby:OpenCo"], "2026-12-15"), "rechecked after 30 days")
        finally:
            shutil.rmtree(d)


class NewSystems(unittest.TestCase):
    def test_adapters_parse_live_shapes(self):
        samples = {
            "https://apply.workable.com/api/v1/widget/accounts/acme?details=true": {"name": "Acme", "jobs": [{"title": "Financial Analyst", "shortcode": "AB12", "telecommuting": False, "url": "https://apply.workable.com/j/AB12",
                "published_on": "2026-10-27", "locations": [{"country": "United States", "city": "White Plains", "region": "New York", "hidden": False}], "description": "<p>Own the forecast</p>"}]},
            "https://acme.breezy.hr/json": [{"id": "1d4", "name": "FP&A Analyst", "url": "https://acme.breezy.hr/p/1d4-fpa", "published_date": "2026-10-16T20:47:42Z",
                "location": {"country": {"name": "United States", "id": "US"}, "state": {"id": "NJ", "name": "New Jersey"}, "city": "Hoboken", "is_remote": False}, "salary": "$90,000 – $110,000 / year", "company": {"name": "Acme"}}],
            "https://acme.breezy.hr/p/1d4-fpa": '<html><div class="description"><p>Build the budget</p></div><div class="apply">x</div></html>',
            "https://acme.recruitee.com/api/offers/": {"offers": [{"id": 7, "title": "Finance Associate", "company_name": "Acme", "careers_url": "https://acme.recruitee.com/o/fa", "published_at": "2026-10-07 12:20:15 UTC",
                "locations": [{"city": "New York", "state": "New York", "country": "United States"}], "remote": False, "description": "<p>Plan</p>", "requirements": "<p>Excel</p>"}]},
            "https://acme.teamtailor.com/jobs.json": {"title": "Acme", "items": [{"id": "u-1", "title": "Strategic Finance Analyst", "url": "https://acme.teamtailor.com/jobs/1", "date_published": "2026-10-27T13:19:32+00:00",
                "content_html": "<p>Models</p>", "_jobposting": {"jobLocation": [{"address": {"addressLocality": "Brooklyn", "addressRegion": "NY", "addressCountry": "US"}}]}}]},
            "https://acme.pinpointhq.com/postings.json": {"data": [{"id": "43", "title": "Budget Analyst", "url": "https://acme.pinpointhq.com/en/postings/x", "workplace_type": "hybrid",
                "location": {"city": "Stamford", "province": "CT", "name": "Stamford, CT"}, "description": "<div>Budgets</div>", "compensation_minimum": 40, "compensation_maximum": 50, "compensation_frequency": "hour"}]},
            "https://ats.rippling.com/api/v2/board/acme/jobs?page=0&pageSize=50": {"items": [{"id": "68e6", "name": "Financial Analyst", "url": "https://ats.rippling.com/acme/jobs/68e6",
                "locations": [{"name": "Jersey City, NJ", "country": "United States", "stateCode": "NJ", "city": "Jersey City", "workplaceType": "HYBRID"}]}], "totalPages": 1},
            "https://ats.rippling.com/api/v2/board/acme/jobs/68e6": {"description": "<p>Forecasting</p>"},
            "https://acme.jobs.personio.de/search.json": [{"id": 78, "name": "Controller", "offices": ["Berlin"], "office": "Berlin", "description": ""}],
        }

        def api(url, data=None, want_json=True, **k):
            return samples[url]
        for board, loc, desc in (("workable:acme", "White Plains, New York, United States", "Own the forecast"),
                                 ("breezy:acme", "Hoboken, NJ, United States", "Build the budget"),
                                 ("recruitee:acme", "New York, New York, United States", "Plan"),
                                 ("teamtailor:acme", "Brooklyn, NY, US", "Models"),
                                 ("pinpoint:acme", "Stamford, CT", "Budgets"),
                                 ("rippling:acme", "Jersey City, NJ, United States", "Forecasting"),
                                 ("personio:acme", "Berlin", "")):
            jobs = ats.fetch_board(board, D.want, api)
            self.assertEqual(jobs[0]["location"], loc, board)
            self.assertIn(desc, jobs[0]["description"], board)
            self.assertTrue(jobs[0]["id"].startswith(board.split(":")[0] + ":"))
            self.assertEqual(ats.board_of(jobs[0]["url"]) or board, board, board)
        self.assertEqual(ats.fetch_board("breezy:acme", D.want, api)[0]["pay_max"], "110000")
        self.assertTrue(scrape.NATIVE.match("teamtailor:u-1"))

    def test_board_of_skips_junk(self):
        self.assertEqual(ats.board_of("https://jobs.smartrecruiters.com/external-referrals/company/X"), "")
        self.assertEqual(ats.board_of("https://3m.wd1.myworkdayjobs.com/en"), "")
        self.assertEqual(ats.board_of("https://www.breezy.hr/pricing"), "")
        self.assertEqual(ats.board_of("https://jobs.lever.co/acme/1"), "lever:acme")


class RequestManager(unittest.TestCase):
    def test_retry_after_and_partial_workday(self):
        waits, calls = [], []
        ats.time.sleep = lambda s: waits.append(s)

        def opener(req, timeout=20):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "slow", {"Retry-After": "7"}, io.BytesIO(b""))
            return io.BytesIO(b'{"ok": 1}')
        try:
            self.assertEqual(ats.http("https://api.lever.co/v0/postings/x", opener=opener), {"ok": 1})
            self.assertIn(7.0, waits, "Retry-After honoured")
            self.assertGreaterEqual(ats.STATS["api.lever.co"]["throttled"], 1)
        finally:
            ats.time.sleep = lambda s: None

    def test_workday_pages_stop_on_repeats(self):
        posts = [{"title": f"Financial Analyst {i}", "externalPath": f"/job/NY/FA_R{i}", "locationsText": "New York, NY"} for i in range(20)]
        calls = []

        def api(url, data=None, **k):
            calls.append(data and data.get("offset"))
            if url.endswith("/jobs"):
                return {"total": 100 if data["offset"] == 0 else 0, "jobPostings": posts}      # every page repeats page one
            return {"jobPostingInfo": {}}
        jobs = ats.workday("t|wd1|Ext", lambda j: False, api)
        self.assertEqual(len(jobs), 20)
        self.assertFalse(jobs.complete)
        self.assertLessEqual(calls.count(20), len(ats.WORKDAY_SEARCHES), "second page tried once per search, then stopped")

    def test_still_open(self):
        def gone(url, **k):
            raise ats.HttpError(404, url)
        self.assertFalse(ats.still_open("https://t.wd1.myworkdayjobs.com/Ext/job/NY/FA_R1", gone))
        self.assertTrue(ats.still_open("https://t.wd1.myworkdayjobs.com/en-US/Ext/job/NY/FA_R1", lambda u, **k: {}))
        self.assertIsNone(ats.still_open("https://job-boards.greenhouse.io/acme/jobs/1", gone), "complete boards don't need it")


class History(unittest.TestCase):
    def test_lifecycle(self):
        h = history.load("/nonexistent")
        r = {"id": "greenhouse:1", "title": "FP&A Analyst", "location": "New York, NY", "is_remote": "False", "description": "Forecasts", "date_posted": "2026-10-30"}
        history.update(h, [dict(r)], "2026-11-01")
        rows = [dict(r, min_amount="95000", max_amount="115000", interval="yearly", location="New York, NY (Hybrid)")]
        c = history.update(h, rows, "2026-11-02")
        self.assertEqual((c["changed"], rows[0]["status"]), (1, "changed"))
        self.assertIn("pay added: 95000-115000 yearly", rows[0]["changes"])
        self.assertIn("location:", rows[0]["changes"])
        rows = [dict(rows[0])]
        history.update(h, rows, "2026-11-08")
        self.assertEqual(rows[0]["status"], "unchanged")
        self.assertEqual(rows[0]["age_days"], "9")
        self.assertEqual(history.update(h, [], "2026-11-09", closed=["greenhouse:1"])["closed"], 1)
        rows = [dict(rows[0])]
        self.assertEqual(history.update(h, rows, "2026-11-20")["reappeared"], 1)
        self.assertEqual(rows[0]["status"], "reappeared")

    def test_aggregator_description_noise_ignored(self):
        h = history.load("/nonexistent")
        r = {"id": "in-1", "title": "Financial Analyst", "location": "New York, NY", "description": "copy one"}
        history.update(h, [dict(r)], "2026-11-01")
        rows = [dict(r, description="a different site's copy")]
        self.assertEqual(history.update(h, rows, "2026-11-02")["changed"], 0)


class Requirements(unittest.TestCase):
    def test_extraction(self):
        d = ("3+ years of experience in FP&A or corporate finance. Advanced Excel (pivot tables, XLOOKUP), SQL and Tableau. "
             "Experience with Anaplan is a plus. Bachelor's degree in Finance required; MBA preferred. Build the annual operating plan and monthly forecast.")
        x = reqs.requirements("Senior Financial Analyst", d)
        for s in ("advanced Excel", "SQL", "Tableau", "Anaplan", "annual planning", "forecasting", "MBA"):
            self.assertIn(s, x["skills"])
        self.assertEqual(x["years_min"], "3")
        self.assertEqual(x["education"], "Bachelor's; MBA (preferred)")
        self.assertEqual(x["seniority"], "senior")
        self.assertEqual(reqs.requirements("Financial Analyst", "2-4 years of relevant experience")["years_min"], "2")
        self.assertEqual(reqs.annual_pay({"min_amount": "40", "max_amount": "50", "interval": "hourly"}), ("83200", "104000"))
        self.assertEqual(reqs.annual_pay({"min_amount": "", "max_amount": ""}), ("", ""))


class Closure(unittest.TestCase):
    def test_partial_board_checks_each_missing_job(self):
        d = tempfile.mkdtemp()
        try:
            json.dump({"boards": {"workday:t|wd1|Ext": {"company": "T", "src": "seed"}}}, open(os.path.join(d, "boards.json"), "w"))
            prev = [{c: "" for c in scrape.COLS} | {"id": "workday:R1", "site": "workday", "title": "FP&A Analyst", "company": "T", "location": "New York, NY",
                    "job_url": "https://t.wd1.myworkdayjobs.com/Ext/job/NY/FPA_R1", "last_seen": "2026-10-31", "first_seen": "2026-10-20"},
                    {c: "" for c in scrape.COLS} | {"id": "workday:R2", "site": "workday", "title": "Financial Analyst", "company": "T", "location": "New York, NY",
                    "job_url": "https://t.wd1.myworkdayjobs.com/Ext/job/NY/FA_R2", "last_seen": "2026-10-31", "first_seen": "2026-10-20"}]

            def api(url, data=None, **k):
                if url.endswith("/wday/cxs/t/Ext/jobs"):
                    return {"total": 0, "jobPostings": []}                     # the searches didn't return either job
                if url.endswith("FPA_R1"):
                    raise ats.HttpError(404, url)                              # R1's page is gone
                if url.endswith("FA_R2"):
                    return {"jobPostingInfo": {}}                              # R2's page is still up
                raise ats.HttpError(404, url)
            out, held, stats, closed = scrape.direct_step([], prev, "2026-11-01", os.path.join(d, "boards.json"), api, lambda u: u,
                                                          lambda u: (_ for _ in ()).throw(OSError("offline")))
            self.assertEqual(closed, ["workday:R1"])
            self.assertEqual([r["id"] for r in out], ["workday:R2"])
            self.assertEqual(stats["still_open"], 1)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
