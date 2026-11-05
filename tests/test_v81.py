"""Offline tests for feed v8.1 (Phase 1): the shared Gemini quota, token counts, the saved review queue, verdict reuse and the
false-negative sample."""
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import backlog  # noqa: E402
import discover as D  # noqa: E402
import relevance as R  # noqa: E402
import scrape  # noqa: E402

FPA = ("Own the monthly forecast and the annual budget, with variance analysis against plan for each business unit. Build "
       "driver-based financial models, partner with leadership on headcount planning and prepare the quarterly business review. ") * 2
DULL = ("Run the warehouse schedule for the night shift, check inventory counts, receive deliveries and keep the loading dock "
        "safe and tidy for the team. Coordinate with drivers and keep the shipping log current every day. ") * 2
BORDER = "You will help with the budget and the monthly forecast. " + DULL
UTC = timezone.utc


def row(i, title="Operations Associate", company="Acme", desc=FPA, **k):
    r = {c: "" for c in scrape.COLS}
    r.update(id=i, site=i.split(":")[0], title=title, company=company, location="New York, NY", job_url=f"https://x.test/{i}",
             description=desc, region="New York City", first_seen="2026-11-01")
    r.update(k)
    return r


def verdict(score=88, cat="FP&A / forecasting / budgeting"):
    return {"score": score, "category": cat, "strengths": ["forecasting"], "gap": ""}


def fake_post(verdicts, calls, usage=None, drop=()):
    def post(url, body, token, timeout=60):
        calls.append(url)
        ids = [ln.split('"')[1] for ln in body["contents"][0]["parts"][0]["text"].splitlines() if ln.startswith("<job id=")]
        jobs = [dict(verdicts.get(i) or verdict(30, "Accounting-heavy / unsuitable"), id=i) for i in ids if i not in drop]
        out = {"candidates": [{"content": {"parts": [{"text": json.dumps({"jobs": jobs})}]}}]}
        if usage:
            out["usageMetadata"] = usage
        return out
    return post


def limit(kind):
    def post(url, *a, **k):
        msg = b'{"error":{"details":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}' if kind == "day" else \
            b'{"error":{"details":[{"quotaId":"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}}'
        raise urllib.error.HTTPError(url, 429, "quota", {}, io.BytesIO(msg))
    return post


class Quota(unittest.TestCase):
    def test_pacific_day_and_runs_left(self):
        self.assertEqual(R.pacific_day(datetime(2026, 11, 2, 7, 30, tzinfo=UTC)), "2026-11-01", "07:30 UTC is still Nov 1 in Pacific")
        self.assertEqual(R.pacific_day(datetime(2026, 11, 2, 8, 30, tzinfo=UTC)), "2026-11-02")
        self.assertEqual(R.runs_left(datetime(2026, 11, 2, 11, 45, tzinfo=UTC), "2026-11-02"), 4, "this run + 15, 19, 23 UTC")
        self.assertEqual(R.runs_left(datetime(2026, 11, 2, 23, 45, tzinfo=UTC), "2026-11-02"), 1)

    def test_ceiling_starts_low_steps_up_after_a_clean_day_and_never_passes_the_max(self):
        cache = {"jobs": {}}
        n, u = R.budget(cache, datetime(2026, 11, 2, 11, 45, tzinfo=UTC))
        self.assertEqual((u["ceiling"], n), (R.DAILY_START, 40), "160 a day over 4 runs")
        u["requests"] = 150                                            # used 94% with no limit reply
        n, u = R.budget(cache, datetime(2026, 11, 3, 11, 45, tzinfo=UTC))
        self.assertEqual((u["ceiling"], u["requests"], u["history"][-1]["requests"]), (200, 0, 150))
        for day in range(4, 9):
            u["requests"] = u["ceiling"]
            n, u = R.budget(cache, datetime(2026, 11, day, 11, 45, tzinfo=UTC))
        self.assertEqual(u["ceiling"], R.DAILY_MAX)
        u["requests"] = 10                                             # a quiet day: no step
        n, u = R.budget(cache, datetime(2026, 11, 9, 11, 45, tzinfo=UTC))
        self.assertEqual(u["ceiling"], R.DAILY_MAX)

    def test_a_daily_limit_lowers_the_ceiling_and_stops_the_day(self):
        cache = {"jobs": {}}
        n, u = R.budget(cache, datetime(2026, 11, 2, 11, 45, tzinfo=UTC))
        st = R.run_reviews([row(f"greenhouse:{i}") for i in range(20)], cache, "key", limit("day"), max_calls=n, usage=u, sleep=lambda s: None)
        self.assertEqual((st["limited"], u["limited"], u["requests_at_limit"]), ("day", True, 1))
        self.assertIn("daily limit", st["error"])
        self.assertEqual(R.budget(cache, datetime(2026, 11, 2, 15, 45, tzinfo=UTC))[0], 0, "no more requests this Pacific day")
        u["requests"] = 100
        u["requests_at_limit"] = 100
        n, u = R.budget(cache, datetime(2026, 11, 3, 11, 45, tzinfo=UTC))
        self.assertEqual((u["ceiling"], u["limited"]), (80, False), "80% of what was used when the limit hit")

    def test_a_per_minute_limit_stops_the_run_only(self):
        cache, u = {"jobs": {}}, {}
        st = R.run_reviews([row("greenhouse:1")], cache, "key", limit("minute"), usage=u, sleep=lambda s: None)
        self.assertEqual((st["limited"], bool(u.get("limited"))), ("minute", False))

    def test_a_retired_model_stops_without_falling_back(self):
        urls = []

        def post(url, *a, **k):
            urls.append(url)
            raise urllib.error.HTTPError(url, 404, "gone", {}, io.BytesIO(b""))
        st = R.run_reviews([row("greenhouse:1")], {"jobs": {}}, "key", post, sleep=lambda s: None)
        self.assertEqual(len(urls), 1)
        self.assertIn("CLASSIFY_MODEL", st["error"])
        self.assertTrue(all("flash-lite" in u for u in urls))

    def test_tokens_are_counted_and_eight_jobs_go_in_a_request(self):
        items = [row(f"greenhouse:{i}") for i in range(16)]
        calls, u, cache = [], {}, {"jobs": {}}
        st = R.run_reviews(items, cache, "key", fake_post({}, calls, {"promptTokenCount": 900, "candidatesTokenCount": 300, "totalTokenCount": 1300}),
                           usage=u, sleep=lambda s: None)
        self.assertEqual((st["calls"], st["reviewed"], st["per_request"]), (2, 16, 8))
        self.assertEqual((u["requests"], u["tokens_in"], u["tokens_out"], u["tokens"]), (2, 1800, 600, 2600))

    def test_the_run_stops_when_its_time_is_up(self):
        t = iter(range(0, 10000, 400))
        st = R.run_reviews([row(f"greenhouse:{i}") for i in range(40)], {"jobs": {}}, "key", fake_post({}, []), sleep=lambda s: None,
                           seconds=600, clock=lambda: next(t))
        self.assertEqual((st["calls"], st["stopped"]), (1, "out of time for this run"), "clock 400 s: one call; 800 s: past 600, stop")


class Reuse(unittest.TestCase):
    def test_the_same_posting_elsewhere_reuses_the_verdict(self):
        cache = {"jobs": {}}
        a = row("greenhouse:1", company="Acme")
        R.run_reviews([a], cache, "key", fake_post({"greenhouse:1": verdict()}, []), sleep=lambda s: None)
        twin = row("indeed:9", company="Acme Staffing", desc=FPA.replace(". ", ".\\n").upper(), title="OPERATIONS ASSOCIATE")
        other = row("indeed:10", desc=FPA + " Also own the treasury calendar.")
        self.assertEqual(R.reuse(cache, [twin, other], "2026-11-02"), 1)
        self.assertEqual(R.cached(cache, twin)["score"], 88)
        self.assertTrue(R.cached(cache, twin)["reused"])
        self.assertIsNone(R.cached(cache, other), "a different description is a different posting")

    def test_old_verdicts_get_the_key_from_rows_they_match(self):
        a = row("greenhouse:1")
        cache = {"jobs": {"greenhouse:1": dict(verdict(), score=80, category=R.FPA, why="x", by="model", h=R.digest(a["title"], a["description"]))}}
        R.keyed(cache, [a])
        self.assertEqual(R.reuse(cache, [row("lever:5", company="Other")], "2026-11-02"), 1)

    def test_short_descriptions_are_never_matched(self):
        self.assertEqual(R.norm_key("Analyst", "Budget."), "")


class Backlog(unittest.TestCase):
    def saved(self, i, board, last_seen="2026-10-30", **k):
        return dict(row(i, _board=board, job_url=f"https://x.test/{i}"), held_since="2026-10-25", last_seen=last_seen, **k)

    def test_carry_keeps_jobs_unless_something_shows_they_are_gone(self):
        db = {"boards": {b: {} for b in ("greenhouse:skip", "greenhouse:full", "workday:part", "greenhouse:failed")},
              "held": {"g:1": self.saved("g:1", "greenhouse:skip"), "g:2": self.saved("g:2", "greenhouse:full"),
                       "g:3": self.saved("g:3", "greenhouse:full"), "w:4": self.saved("w:4", "workday:part"),
                       "w:5": self.saved("w:5", "workday:part"), "g:6": self.saved("g:6", "greenhouse:failed"),
                       "g:7": self.saved("g:7", "greenhouse:gone"), "g:8": self.saved("g:8", "greenhouse:skip"),
                       "g:9": self.saved("g:9", "greenhouse:full")}}
        held = {"g:9": row("g:9", _board="greenhouse:full")}
        pages = {"https://x.test/w:4": False, "https://x.test/w:5": True}
        st = backlog.carry(db, held, {"g:8": {}}, {"greenhouse:full": True, "workday:part": False}, {"greenhouse:skip"},
                           {"greenhouse:full": {"g:3"}}, "2026-11-02", lambda u: "", pages.get)
        self.assertEqual(sorted(held), ["g:1", "g:6", "g:9", "w:5"])
        self.assertEqual((st["closed"], st["no_longer_borderline"], st["board_removed"], st["now_in_feed"], st["page_checks"]), (2, 1, 1, 1, 2))
        self.assertEqual(held["w:5"]["last_seen"], "2026-11-02", "its page answered")
        self.assertEqual(held["g:9"]["held_since"], "2026-10-25", "seen again: keeps its place in line")

    def test_store_saves_only_unreviewed_jobs_with_a_digest_compatible_description(self):
        db = {}
        long_desc = FPA * 10
        held = {"g:1": row("g:1", desc=long_desc, _board="greenhouse:a"), "g:2": row("g:2", _board="greenhouse:a")}
        self.assertEqual(backlog.store(db, held, lambda r: r["id"] == "g:2", "2026-11-02", scrape.COLS), 1)
        saved = db["held"]["g:1"]
        self.assertEqual(len(saved["description"]), backlog.KEEP_CHARS)
        self.assertEqual(R.digest(saved["title"], saved["description"]), R.digest("Operations Associate", long_desc))
        self.assertEqual((saved["held_since"], saved["_board"]), ("2026-11-02", "greenhouse:a"))

    def test_order_puts_strong_jobs_first_and_waiting_time_keeps_adding(self):
        strong = row("g:1", held_since="2026-11-02")
        weak_new = row("g:2", held_since="2026-11-02")
        weak_old = row("g:3", held_since="2026-10-12")
        v = lambda r, core: backlog.value(r, "2026-11-02", core)
        self.assertGreater(v(strong, 5), v(weak_new, 2))
        self.assertGreater(v(weak_old, 2), v(strong, 5), "20 days waiting outranks a strong new arrival: nothing starves")
        self.assertEqual(backlog.oldest_wait([strong, weak_old], "2026-11-02"), 21)

    def test_sample_is_repeatable_and_bounded(self):
        pool = list(range(100))
        self.assertEqual(backlog.sample(pool, 16, "a"), backlog.sample(pool, 16, "a"))
        self.assertEqual(len(backlog.sample(pool, 16, "a")), 16)
        self.assertEqual(backlog.sample([1, 2], 16, "a"), [1, 2])


class BoardStep(unittest.TestCase):
    def test_rules_tag_only_description_drops(self):
        j = {"title": "Operations Associate", "location": "New York, NY", "remote": False, "company": "Acme", "description": DULL}
        self.assertEqual((D.judge(j), j.get("_drop")), ("drop", "few planning duties"))
        k = {"title": "Software Engineer", "location": "New York, NY", "remote": False, "company": "Acme", "description": DULL}
        self.assertEqual((D.judge(k), k.get("_drop")), ("drop", None), "a title the feed never reads is not a rules judgment")

    def test_held_queue_survives_a_run_where_its_board_is_skipped(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "boards.json")
            json.dump({"boards": {"greenhouse:acme": {"company": "Acme", "src": "seed"}}}, open(path, "w"))
            listing = [{"id": 1, "title": "Operations Associate", "location": {"name": "New York, NY"}, "content": "<p>" + BORDER + "</p>",
                        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1", "company_name": "Acme"},
                       {"id": 2, "title": "Operations Associate II", "location": {"name": "New York, NY"}, "content": "<p>" + DULL + "</p>",
                        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/2", "company_name": "Acme"}]

            def api(url, data=None, **k):
                if url.endswith("/acme/jobs?content=true"):
                    return {"jobs": listing}
                raise OSError("offline")
            offline = lambda u: (_ for _ in ()).throw(OSError("offline"))
            out, held, st, _ = scrape.direct_step([], [], "2026-11-01", path, api, lambda u: u, offline, cache={"jobs": {}})
            self.assertEqual([h["id"] for h in held], ["greenhouse:1"])
            self.assertEqual([a["id"] for a in st["_audit"]], ["greenhouse:2"], "the rules' reject goes to the sample")
            self.assertIn("greenhouse:1", json.load(open(path))["held"])
            db = json.load(open(path))                                     # next run: the board is skipped as unchanged
            db["boards"]["greenhouse:acme"]["next"] = 10 ** 6
            db["boards"]["greenhouse:acme"]["ok"] = "2026-11-01"
            json.dump(db, open(path, "w"))
            out, held, st, _ = scrape.direct_step([], [], "2026-11-02", path, api, lambda u: u, offline, cache={"jobs": {}})
            self.assertEqual([h["id"] for h in held], ["greenhouse:1"], "carried, not dropped for lack of review")
            self.assertEqual(st["held"]["carried"], 1)
            self.assertEqual(json.load(open(path))["held"]["greenhouse:1"]["held_since"], "2026-11-01")
        finally:
            shutil.rmtree(d)


class ClassifyJob(unittest.TestCase):
    def run_job(self, review, verdicts, cache=None, drop=()):
        d = tempfile.mkdtemp()
        try:
            feed = [row("indeed:1", title="Financial Analyst", desc=FPA)]
            with open(os.path.join(d, "jobs.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=scrape.COLS, extrasaction="ignore")
                w.writeheader()
                w.writerows(feed)
            json.dump(review, open(os.path.join(d, "review.json"), "w"))
            json.dump({"ran_at": "x"}, open(os.path.join(d, "feed_status.json"), "w"))
            cpath = os.path.join(d, "cache_in.json")
            json.dump(cache or {"v": 1, "jobs": {}}, open(cpath, "w"))
            calls = []
            R.time.sleep = lambda s: None
            st = R.main(d, cpath, fake_post(verdicts, calls, drop=drop), token="tok", today="2026-11-02",
                        now=datetime(2026, 11, 2, 11, 45, tzinfo=UTC))
            out = {r["id"]: r for r in csv.DictReader(open(os.path.join(d, "jobs.csv")))}
            saved = json.load(open(os.path.join(d, "classify_cache.json")))
            status = json.load(open(os.path.join(d, "feed_status.json")))["classifier"]
            return st, out, saved, status, calls
        finally:
            shutil.rmtree(d)

    def test_sample_first_twins_once_fits_rescued_and_usage_published(self):
        held = [dict(row(f"greenhouse:{i}", desc=FPA + f" Team {i}."), held_since="2026-10-28") for i in range(3)]
        twin = dict(row("lever:7", company="Acme Staffing", desc=FPA + " Team 0."), held_since="2026-11-02")
        sample = [dict(row("greenhouse:50", title="Revenue Operations Manager", desc=FPA + " Revenue."), _drop="few planning duties"),
                  dict(row("greenhouse:51", title="Store Manager", desc=DULL), _drop="few planning duties")]
        verdicts = {"greenhouse:0": verdict(), "greenhouse:50": verdict(82, "Corporate / strategic finance")}
        st, out, saved, status, calls = self.run_job({"review_ids": [], "borderline": held + [twin], "audit": sample,
                                                      "held_ids": ["greenhouse:0", "greenhouse:1", "greenhouse:2", "lever:7"]}, verdicts)
        self.assertEqual(len(calls), 2, "1 request for the sample, 1 for 3 held jobs (the twin isn't sent)")
        self.assertIn("greenhouse:50", out, "a fit the rules turned away is rescued into the feed")
        self.assertIn("greenhouse:0", out)
        self.assertEqual(out["lever:7"]["fit_by"], "model", "the twin got its verdict without a review")
        self.assertNotIn("greenhouse:51", out)
        self.assertEqual(st["reused"], 1)
        fn = status["false_negatives"]
        self.assertEqual((fn["sampled"], fn["reviewed"], [f["title"] for f in fn["fits"]]), (2, 2, ["Revenue Operations Manager"]))
        self.assertEqual((status["usage"]["requests"], status["usage"]["ceiling"]), (2, R.DAILY_START))
        self.assertEqual((status["held_waiting"], status["held_oldest_days"]), (0, 0))
        self.assertEqual(saved["usage"]["requests"], 2, "the day's count is saved for the next run")
        self.assertTrue(all(v.get("n") for v in saved["jobs"].values() if not v.get("reused")))

    def test_flag_when_many_sampled_rejects_are_fits(self):
        sample = [dict(row(f"greenhouse:{i}", desc=FPA + f" Unit {i}."), _drop="few planning duties") for i in range(16)]
        verdicts = {f"greenhouse:{i}": verdict() for i in range(16)}
        st, out, saved, status, calls = self.run_job({"review_ids": [], "borderline": [], "audit": sample}, verdicts)
        self.assertIn("flag", status["false_negatives"])

    def test_waiting_jobs_report_their_age_when_the_day_is_used_up(self):
        held = [dict(row(f"greenhouse:{i}", desc=FPA + f" Desk {i}."), held_since="2026-10-20") for i in range(3)]
        cache = {"v": 1, "jobs": {}, "usage": {"day": "2026-11-02", "requests": 160, "ceiling": 160}}
        st, out, saved, status, calls = self.run_job({"review_ids": [], "borderline": held}, {}, cache)
        self.assertEqual((len(calls), status["held_waiting"], status["held_oldest_days"]), (0, 3, 13))

    def test_smaller_requests_when_replies_leave_jobs_out(self):
        held = [dict(row(f"greenhouse:{i}", desc=FPA + f" Region {i}."), held_since="2026-11-01") for i in range(16)]
        st, out, saved, status, calls = self.run_job({"review_ids": [], "borderline": held}, {}, drop={f"greenhouse:{i}" for i in range(0, 16, 2)})
        self.assertEqual(saved["usage"]["per_request"], 4)


if __name__ == "__main__":
    unittest.main()
