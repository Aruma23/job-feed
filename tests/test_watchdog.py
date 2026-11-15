"""Offline tests for watchdog.py: failure reading, findings, rendering."""
import os
import sys
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import watchdog as W  # noqa: E402

NOW = datetime(2026, 11, 14, 13, 0, tzinfo=timezone.utc)
OK_STATUS = {"ran_at": "2026-11-14T12:40:00+00:00", "searches": 28, "failed": 0,
             "direct": {"boards_due": 534, "boards_ok": 532, "sweep_seconds": 600,
                        "adapter_health": {"greenhouse": {"due": 500, "read": 499, "late": 0, "state": "ok"}}},
             "contract": {"quality": {"a_b_accounting": 0, "a_b_intermediary": 0}}, "audit": {"level": "warn"}, "classifier": {}}
RUN_OK = {"status": "completed", "conclusion": "success", "run_number": 81, "id": 1}
RUN_BAD = {"status": "completed", "conclusion": "failure", "run_number": 80, "id": 2}
JOBS_CHECK = [{"name": "collect", "conclusion": "failure", "steps": [{"name": "Run python scrape.py", "conclusion": "success"},
              {"name": "Check the output before handing it on", "conclusion": "failure"}]}]
LOG_DUP = ("2026-11-14T12:42:53.0599035Z Traceback (most recent call last):\n"
           "2026-11-14T12:42:53.0605647Z AssertionError: two rows share an id (CONTRACT.md invariant 1)\n"
           "2026-11-14T12:42:53.1Z ##[error]Process completed with exit code 1.\n")


class Failure(unittest.TestCase):
    def test_nov14_crash_is_read_and_not_rerun(self):
        job, step, err, transient = W.failure_of(JOBS_CHECK, LOG_DUP)
        self.assertEqual(step, "Check the output before handing it on")
        self.assertIn("two rows share an id", err)
        self.assertFalse(transient, "a failed check fails again on the same code")

    def test_network_failure_is_transient(self):
        jobs = [{"name": "collect", "conclusion": "failure", "steps": [{"name": "Run python scrape.py", "conclusion": "failure"}]}]
        self.assertTrue(W.failure_of(jobs, "x Z urllib.error.URLError: <urlopen error [Errno 104] Connection reset by peer>")[3])
        timed = [{"name": "collect", "conclusion": "timed_out", "steps": [{"name": "Run python scrape.py", "conclusion": "cancelled"}]}]
        self.assertTrue(W.failure_of(timed, "")[3])


class Findings(unittest.TestCase):
    def test_healthy_is_clear(self):
        self.assertEqual(W.check(OK_STATUS, [RUN_OK], NOW), [])

    def test_failed_runs_and_streak(self):
        f = W.check(OK_STATUS, [RUN_BAD, dict(RUN_BAD, run_number=79), RUN_OK], NOW, W.failure_of(JOBS_CHECK, LOG_DUP))
        self.assertEqual(f[0]["key"], "run-failed")
        self.assertIn("2 failed in a row", f[0]["title"])
        self.assertIn("Fix the cause in code", f[0]["todo"])

    def test_silent_workday_and_budget(self):
        st = dict(OK_STATUS, direct=dict(OK_STATUS["direct"], sweep_seconds=750,
                  adapter_health={"workday": {"due": 0, "read": 0, "late": 327, "state": "ok"}}))
        keys = [x["key"] for x in W.check(st, [RUN_OK], NOW)]
        self.assertIn("system-zero-workday", keys, "the Nov 10-14 starvation would have been red on day one")
        self.assertIn("sweep-budget", keys)

    def test_idle_system_as_the_old_code_reported_it(self):
        import json
        st = json.load(open(os.path.join(os.path.dirname(HERE), "feed_status.json")))
        if "workday" in st.get("direct", {}).get("adapter_health", {}) and not st["direct"]["adapter_health"]["workday"].get("read"):
            keys = [x["key"] for x in W.check(st, [RUN_OK], NOW)]
            self.assertIn("system-idle-workday", keys, "the real Nov 14 feed_status")
        st = dict(OK_STATUS, direct=dict(OK_STATUS["direct"], adapter_health={"workday": {"due": 0, "read": 0, "skipped": 0, "since": "2026-11-01"}}))
        self.assertEqual(W.check(st, [RUN_OK], NOW)[0]["key"], "system-idle-workday")

    def test_stale_and_slow(self):
        st = dict(OK_STATUS, ran_at="2026-11-14T00:40:00+00:00")
        keys = [x["key"] for x in W.check(st, [RUN_OK], NOW, collect_minutes=45)]
        self.assertEqual(keys[0], "stale")
        self.assertIn("collect-slow", keys)

    def test_quality_and_duplicates(self):
        st = dict(OK_STATUS, duplicate_ids_merged={"count": 2, "ids": ["in-1"]},
                  contract={"quality": {"a_b_accounting": 1, "a_b_intermediary": 0}})
        keys = {x["key"] for x in W.check(st, [RUN_OK], NOW)}
        self.assertEqual(keys, {"duplicate-ids", "quality"})

    def test_render_carries_keys(self):
        body = W.render(W.check(OK_STATUS, [RUN_BAD], NOW, W.failure_of(JOBS_CHECK, LOG_DUP)), OK_STATUS, [RUN_BAD])
        self.assertIn("watchdog keys: run-failed", body)


if __name__ == "__main__":
    unittest.main()
