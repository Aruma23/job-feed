"""Offline tests for scrape.py: a fake JobSpy, no network. Run: python -m unittest discover -s tests"""
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
import scrape  # noqa: E402

scrape.random.uniform = lambda a, b: 0
scrape.time.sleep = lambda s: None
scrape.DIRECT_DEFAULT = False   # these tests cover the aggregator part; test_direct.py covers the boards


class FakeDF:
    def __init__(self, recs):
        self.recs = recs

    def to_dict(self, orient):
        return self.recs


def job(i, site, title, company, direct="", desc="", url=None):
    return {"id": f"{site[:2]}-{i}", "site": site, "title": title, "company": company,
            "location": "New York, NY", "is_remote": False, "date_posted": "2026-10-30",
            "job_url": url or f"https://{site}.example/{i}", "job_url_direct": direct,
            "min_amount": float("nan"), "max_amount": 120000.0, "interval": "yearly",
            "description": desc}


def fake(table, calls=None):
    def scrape_jobs(**kw):
        if calls is not None:
            calls.append(kw)
        key = (kw["site_name"][0], kw.get("search_term") or kw.get("google_search_term"))
        v = table.get(key, [])
        if isinstance(v, Exception):
            raise v
        return FakeDF(v)
    return scrape_jobs


def read(path):
    csv.field_size_limit(10 ** 8)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "jobs.csv")
        self.st = os.path.join(self.d, "feed_status.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_feed(self, table, day="2026-10-31", calls=None, searches=None):
        old = scrape.SEARCHES
        if searches:
            scrape.SEARCHES = searches
        try:
            now = datetime.fromisoformat(day + "T12:00:00+00:00")
            return scrape.main(scrape=fake(table, calls), out=self.out, status_path=self.st, now=now)
        finally:
            scrape.SEARCHES = old

    def test_failure_isolated_and_marked_partial(self):
        s = [("indeed", "a", 5), ("linkedin", "b", 5)]
        summ = self.run_feed({("indeed", "a"): RuntimeError("429 blocked"),
                              ("linkedin", "b"): [job(1, "linkedin", "FP&A Analyst", "Acme")]}, searches=s)
        self.assertEqual(len(read(self.out)), 1)
        self.assertTrue(summ["partial"])
        self.assertEqual(json.load(open(self.st))["failed"], 1)

    def test_linkedin_asks_for_description_and_google_uses_its_term(self):
        calls = []
        self.run_feed({}, calls=calls, searches=[("linkedin", "x", 5), ("google", "x jobs near New York", 5)])
        self.assertTrue(calls[0]["linkedin_fetch_description"])
        self.assertEqual(calls[1]["google_search_term"], "x jobs near New York")
        self.assertNotIn("search_term", calls[1])

    def test_cross_site_duplicate_keeps_first_id_and_best_links(self):
        s = [("indeed", "a", 5), ("linkedin", "b", 5)]
        self.run_feed({("indeed", "a"): [job(1, "indeed", "FP&A Analyst", "Acme Inc.")]}, searches=s)
        first_id = read(self.out)[0]["id"]
        self.run_feed({("linkedin", "b"): [job(9, "linkedin", "FP&A analyst", "ACME",
                                               direct="https://boards.greenhouse.io/acme/1", desc="Build models")]},
                      day="2026-11-01", searches=s)
        rows = read(self.out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first_id)
        self.assertEqual(rows[0]["job_url_direct"], "https://boards.greenhouse.io/acme/1")
        self.assertEqual(rows[0]["description"], "Build models")
        self.assertEqual(rows[0]["also_on"], "linkedin")
        self.assertEqual(rows[0]["first_seen"], "2026-10-31")

    def test_blocked_run_keeps_earlier_jobs_then_they_age_out(self):
        s = [("indeed", "a", 5)]
        self.run_feed({("indeed", "a"): [job(1, "indeed", "Financial Analyst", "Beta")]}, searches=s)
        self.run_feed({("indeed", "a"): RuntimeError("blocked")}, day="2026-11-02", searches=s)
        self.assertEqual(len(read(self.out)), 1)
        self.run_feed({("indeed", "a"): RuntimeError("blocked")}, day="2026-11-06", searches=s)
        self.assertEqual(len(read(self.out)), 0)

    def test_v1_feed_upgrades_in_place(self):
        shutil.copy(os.path.join(HERE, "v1_sample.csv"), self.out)
        before = read(self.out)
        self.run_feed({}, searches=[("indeed", "a", 5)])
        after = read(self.out)
        self.assertGreater(len(after), 0)
        self.assertLessEqual(len(after), len(before))
        self.assertTrue(set(r["id"] for r in after) <= set(r["id"] for r in before))
        self.assertTrue(all(r["first_seen"] == "2026-10-31" for r in after))
        self.assertEqual(list(after[0].keys())[:13], scrape.COLS[:13])

    def test_blank_values_and_newlines_are_cleaned(self):
        self.run_feed({("indeed", "a"): [job(1, "indeed", "FP&A Analyst", "Acme", desc="line1\nline2")]},
                      searches=[("indeed", "a", 5)])
        r = read(self.out)[0]
        self.assertEqual(r["min_amount"], "")
        self.assertEqual(r["max_amount"], "120000")
        self.assertEqual(r["description"], "line1\\nline2")


if __name__ == "__main__":
    unittest.main()
