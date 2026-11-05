"""Offline tests for v8: the collection footprint (unchanged boards read less often, productive ones every run, never more
than 2 days apart, failure backoff, skipped boards' jobs kept) and the source graph (sources.json)."""
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
import footprint as F  # noqa: E402
import scrape  # noqa: E402

ats.time.sleep = lambda s: None
D.time.sleep = lambda s: None
TODAY = "2026-11-01"


def jobs(n, title="Engineer"):
    return [{"id": f"greenhouse:{i}", "title": title, "location": "Boston"} for i in range(n)]


class Profile(unittest.TestCase):
    def test_unchanged_board_backs_off_and_resets(self):
        info = {}
        F.after_read(info, jobs(120), [], 1, TODAY)
        self.assertEqual((info["every"], info["next"]), (1, 2), "first read: changed")
        F.after_read(info, jobs(120), [], 2, TODAY)
        self.assertEqual(info["every"], 2)
        self.assertFalse(F.due(dict(info, ok=TODAY), 3, TODAY), "skipped while unchanged")
        F.after_read(info, jobs(120), [], 4, TODAY)
        F.after_read(info, jobs(120), [], 8, TODAY)
        F.after_read(info, jobs(120), [], 16, TODAY)
        self.assertEqual(info["every"], F.MAX_EVERY, "capped")
        F.after_read(info, jobs(121), [], 24, TODAY)
        self.assertEqual(info["every"], 1, "a change resets it")

    def test_board_with_recent_match_waits_at_most_two_runs(self):
        info = {"last_hit": "2026-10-30"}
        for run in range(1, 6):
            F.after_read(info, jobs(50), [], run, TODAY)
        self.assertEqual(info["every"], F.MAX_EVERY_HOT)

    def test_new_fitting_job_means_every_run(self):
        info = {}
        for run in range(1, 5):
            F.after_read(info, jobs(50), [], run, TODAY)
        hit = {"id": "greenhouse:99", "title": "FP&A Analyst", "location": "New York, NY", "description": "x"}
        n = F.after_read(info, jobs(50) + [hit], [hit], 5, TODAY)
        self.assertEqual((n, info["every"], info["yield"][-1]), (1, 1, 1))
        self.assertEqual(F.after_read(info, jobs(50) + [hit], [hit], 6, TODAY), 0, "the same job isn't new twice")
        self.assertEqual(info["fields"]["desc"], 1.0)

    def test_never_more_than_two_days(self):
        self.assertTrue(F.due({"next": 99, "ok": "2026-10-30"}, 5, TODAY))
        self.assertFalse(F.due({"next": 99, "ok": "2026-10-31"}, 5, TODAY))

    def test_failure_backoff(self):
        info = {"fails": 2}
        F.after_failure(info, "HttpError: 503", 10)
        self.assertEqual((info["every"], info["next"]), (4, 14))
        self.assertEqual(info["errs"], ["HttpError: 503"])

    def test_requests_counted_by_host(self):
        ats.STATS.clear()
        ats.STATS.update({"boards-api.greenhouse.io": {"requests": 40}, "acme.wd5.myworkdayjobs.com": {"requests": 6}})
        r = F.report({"greenhouse": {"read": 30, "new": 4}, "workday": {"read": 2, "new": 3}}, 5, 7)
        self.assertEqual((r["by_system"]["greenhouse"]["per_100_requests"], r["by_system"]["workday"]["requests"]), (10.0, 6))
        ats.STATS.clear()

    def test_endpoints(self):
        self.assertEqual(F.endpoint("greenhouse:acme"), "https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        self.assertEqual(F.endpoint("workday:t|wd5|Ext"), "https://t.wd5.myworkdayjobs.com/wday/cxs/t/Ext/jobs")


class Sweep(unittest.TestCase):
    def test_unchanged_board_skipped_and_its_jobs_kept(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "boards.json")
            board = {"company": "Acme", "src": "seed", "sig": F.signature([{"id": "greenhouse:5", "title": "FP&A Analyst", "location": "New York, NY"}]),
                     "same": 3, "every": 8, "next": 50, "ok": TODAY}
            json.dump({"boards": {"greenhouse:acme": board}, "run": 10}, open(p, "w"))
            prev = [dict({c: "" for c in scrape.COLS}, id="greenhouse:5", site="greenhouse", company="Acme", title="FP&A Analyst",
                         location="New York, NY", job_url="https://boards.greenhouse.io/acme/jobs/5", last_seen="2026-10-31",
                         first_seen="2026-10-20")]
            calls = []

            def api(url, data=None, **k):
                calls.append(url)
                raise ats.HttpError(404, url)
            out, held, stats, closed = scrape.direct_step([], prev, TODAY, p, api, lambda u: u, lambda u: (_ for _ in ()).throw(OSError("offline")))
            self.assertFalse([c for c in calls if "boards/acme" in c], "no request for an unchanged board that isn't due")
            self.assertEqual(([r["id"] for r in out], closed), (["greenhouse:5"], []))
            self.assertEqual(stats["kept_not_due"], 1)
            self.assertEqual(stats["footprint"]["skipped_unchanged"], 1)
        finally:
            shutil.rmtree(d)


class Graph(unittest.TestCase):
    def test_source_graph_roles(self):
        d = tempfile.mkdtemp()
        try:
            db = {"boards": {"greenhouse:acme": {"company": "Acme", "nyc": 5, "hits": 4, "yield": [1, 0, 2], "ok": TODAY},
                             "workday:acme|wd1|Ext": {"company": "Acme", "nyc": 5, "hits": 0, "yield": [0], "ok": TODAY},
                             "lever:dead": {"company": "Dead Co", "nyc": 2, "fails": 4, "hits": 1},
                             "lever:idle": {"company": "Idle Inc", "nyc": 3}},
                  "employers": {"acme": {"domain": "acme.com", "linked": ["greenhouse:acme"]}}}
            rows = [dict(id="greenhouse:1", company="Acme", fit_score="80")]
            s = F.source_graph(db, rows, [{"host": "jcp.com", "qualified": 4}], TODAY, os.path.join(d, "sources.json"))
            g = json.load(open(os.path.join(d, "sources.json")))["employers"]
            self.assertEqual([b["role"] for b in g["acme"]["boards"]], ["primary", "secondary"])
            self.assertEqual(g["acme"]["site_links_board"], ["greenhouse:acme"])
            self.assertEqual(g["acme"]["fitting_jobs"], ["greenhouse:1"])
            self.assertEqual(g["dead"]["boards"][0]["role"], "failing")
            self.assertIn("site:jcp.com", g)
            self.assertNotIn("idle", g, "never matched and nothing fitting now: left out to keep the file small")
            self.assertEqual(s["employers_with_2_boards"], 1)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
