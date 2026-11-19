"""Nov 18 2026: fixes from a review of an outside audit, each checked against the published feed first.

Every test here fails on the code before this change.
"""
import json
import os
import re
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ats  # noqa: E402
import employer  # noqa: E402
import harvest  # noqa: E402
import priority  # noqa: E402
import relevance as R  # noqa: E402

DUTIES = ("Own the monthly forecast and the annual budget. Prepare variance analysis against plan and present it to "
          "business partners. Build driver-based models for headcount and revenue.")
GENERATED = ("jobs.csv", "feed_status.json", "boards.json", "search_stats.json", "review.json", "registry.tsv",
             "harvest_state.json", "history.json", "AUDIT.md", "employers.json", "audit_log.json", "sources.json",
             "classify_cache.json")


class TestsReadNoPublishedData(unittest.TestCase):
    def test_no_test_opens_a_file_the_feed_rewrites(self):
        # Nov 16-18: test_watchdog read the published feed_status.json; the 2:06 pm run rewrote it, the test failed, and the
        # scheduled scrape (tests first) stopped publishing for two days. Tests use fixtures or temporary directories.
        bad = []
        for name in sorted(os.listdir(HERE)):
            if not name.endswith(".py") or name == os.path.basename(__file__):
                continue
            for i, line in enumerate(open(os.path.join(HERE, name), encoding="utf-8"), 1):
                if re.search(r"dirname\(HERE\)|\bROOT\b", line) and any(f'"{g}"' in line for g in GENERATED):
                    bad.append(f"{name}:{i}")
        self.assertEqual(bad, [], "a test reads data the feed rewrites every run")


class TitleOnly(unittest.TestCase):
    def test_missing_description_adds_nothing(self):
        v = R.rules("FP&A Analyst", "", "strong")
        self.assertEqual(v["score"], 50, "was 60: the title alone beat the 50 bar by 10 points")
        self.assertEqual(R.rules("Finance Analyst", "", "broad")["score"], 38)

    def test_title_only_is_held_at_c(self):
        v = R.rules("FP&A Analyst", "", "strong")
        row = {"fit_score": "75", "fit_why": v["why"], "fit_category": R.FPA, "freshness": "fresh", "trust": "verified",
               "region": "New York", "status": "new", "seniority": "analyst", "employer_type": "direct"}
        self.assertEqual(priority.score(row)[1], "C")
        row["fit_why"] = "Strong match: forecasting, budgeting."
        self.assertEqual(priority.score(row)[1], "A", "the same row with duties to check is not held")


class UngroundedModel(unittest.TestCase):
    def test_score_held_below_rescue_when_no_evidence_is_in_the_posting(self):
        def post(url, body, token):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"jobs": [
                {"id": "j1", "score": 85, "category": R.FPA, "strengths": ["capital markets syndication"], "gap": ""},
                {"id": "j2", "score": 85, "category": R.FPA, "strengths": ["variance analysis"], "gap": ""}]})}]}}]}
        out = R.review([("j1", "Analyst", "Acme", DUTIES), ("j2", "Analyst", "Acme", DUTIES)], "k", "m", post)
        self.assertEqual(out["j1"]["score"], R.UNGROUNDED_CAP)
        self.assertFalse(R.is_fit(out["j1"]), "an ungrounded verdict can't rescue a job")
        self.assertEqual(out["j2"]["score"], 85, "grounded evidence keeps the model's score")


class MassPosting(unittest.TestCase):
    def rows(self):
        desc = "Prepare the monthly close support package and partner with regional leaders on reporting. " * 4
        copies = [{"company": "Big Bank", "title": "Financial Analyst", "id": f"workday:R{i}", "trust": "consistent",
                   "description": desc + f" Location {i}."} for i in range(8)]
        other = {"company": "Big Bank", "title": "FP&A Manager", "id": "workday:R99", "trust": "consistent",
                 "description": DUTIES * 3}
        return copies, other

    def test_one_repeated_title_does_not_mark_the_employer(self):
        copies, other = self.rows()
        employer.annotate(copies + [other])
        self.assertEqual(copies[0]["employer_type"], "marketplace", "the repeated cluster itself is still flagged")
        self.assertEqual(other["employer_type"], "direct", "was marketplace: every role at the employer inherited the flag")

    def test_employer_site_linking_the_board_beats_the_copy_count(self):
        copies, _ = self.rows()
        for r in copies:
            r["trust"] = "verified"
        employer.annotate(copies)
        self.assertEqual(copies[0]["employer_type"], "direct")


class RegistryStatus(unittest.TestCase):
    def test_only_404_and_410_are_gone(self):
        self.assertEqual([harvest.status_of(c) for c in (404, 410, 403, 401, 400, 500)],
                         ["gone", "gone", "blocked", "blocked", "refused", "error"])
        for s in ("blocked", "refused"):
            self.assertLess(harvest.RECHECK_DAYS[s], harvest.RECHECK_DAYS["gone"])

    def test_verify_records_a_403_as_blocked(self):
        def get(url, **k):
            raise ats.HttpError(403, url)
        reg = {"greenhouse:acme": {"board": "greenhouse:acme", "status": "new", "checked": "", "local": 0, "jobs": 0, "seen": 1,
                                   "first": "2026-11-01"}}
        harvest.verify(reg, {"boards": {}}, "2026-11-18", {}, lambda *a: True, lambda t: "strong", lambda *a, **k: None,
                       get=get, seconds=30, limit=5)
        self.assertEqual(reg["greenhouse:acme"]["status"], "blocked")


class OutboundLinks(unittest.TestCase):
    def test_private_and_odd_destinations_are_refused(self):
        for u in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8080/", "https://10.1.2.3/x", "file:///etc/passwd",
                  "http://localhost/", "https://[::1]/", "ftp://example.com/", "http://metadata.google.internal/"):
            with self.assertRaises(ats.UnsafeURL, msg=u):
                ats.public_url(u)
        self.assertEqual(ats.public_url("https://93.184.216.34/jobs"), "https://93.184.216.34/jobs")

    def test_a_redirect_into_the_runner_is_not_followed(self):
        h = ats._SafeRedirect()
        req = urllib.request.Request("https://93.184.216.34/r")
        with self.assertRaises(ats.UnsafeURL):
            h.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest/")

    def test_resolve_checks_the_first_link_too(self):
        with self.assertRaises(ats.UnsafeURL):
            ats.resolve_redirect("http://127.0.0.1/r")


class RipplingPage(unittest.TestCase):
    def test_description_comes_from_the_job_page_when_the_api_404s(self):
        page = ('<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"apiData": {
            "jobPost": {"description": {"company": "<p>About Clarity</p>", "role": "<p>Own the monthly forecast and budget.</p>"},
                        "createdOn": "2026-11-12T16:06:13.853000-08:00", "board": {"companyName": "Clarity Clinic"}}}}}})
            + '</script></html>')
        lst = {"items": [{"id": "77", "name": "FP&A Manager", "url": "https://ats.rippling.com/clarity-clinic/jobs/77",
                          "locations": [{"city": "New York", "stateCode": "NY", "country": "United States"}]}], "totalPages": 1}

        def get(url, **k):
            if "pageSize" in url:
                return lst
            if "/api/v2/board/" in url:
                raise ats.HttpError(404, url)
            return page
        jobs = ats.rippling("clarity-clinic", lambda j: True, get)
        self.assertIn("Own the monthly forecast", jobs[0]["description"], "was empty: the detail API 404s")
        self.assertTrue(jobs[0]["description"].index("Own the") < jobs[0]["description"].index("About Clarity"), "role first")
        self.assertEqual(jobs[0]["company"], "Clarity Clinic")
        self.assertEqual(jobs[0]["posted"], "2026-11-12")


class EvidenceGate(unittest.TestCase):
    """Second review (Nov 18): the three ways an unbacked model fit could still reach A/B."""
    LISTING = {"freshness": "fresh", "trust": "verified", "region": "New York", "status": "new", "seniority": "analyst",
               "employer_type": "direct"}

    def verdicts(self, strengths):
        def post(url, body, token):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"jobs": [
                {"id": "j", "score": 88, "category": R.FPA, "strengths": strengths, "gap": ""}]})}]}}]}
        return R.review([("j", "Analyst", "Acme", DUTIES)], "k", "m", post)["j"]

    def tier(self, v):
        row = dict(self.LISTING)
        R.apply(row, v)
        return row["fit_evidence"], priority.score(row)[1]

    def test_no_strengths_at_all_is_unsupported(self):
        v = self.verdicts([])
        self.assertEqual(v["score"], R.UNGROUNDED_CAP, "was 88: an empty list skipped the check")
        self.assertEqual(self.tier(v), ("unsupported", "C"))

    def test_one_grounded_among_several_is_partial_and_not_a(self):
        v = self.verdicts(["variance analysis", "capital markets syndication", "fund formation", "derivatives pricing"])
        self.assertEqual(v["ev"], "partial")
        self.assertEqual(self.tier(v), ("partial", "B"), "was A: one matching phrase let four count")

    def test_held_score_plus_listing_bonuses_does_not_make_an_a(self):
        v = self.verdicts(["capital markets syndication"])
        row = dict(self.LISTING)
        R.apply(row, v)
        total, tier, _ = priority.score(row)
        self.assertGreaterEqual(total, 85, "the bonuses alone would make it an A")
        self.assertEqual(tier, "C")

    def test_well_backed_verdict_is_untouched(self):
        v = self.verdicts(["monthly forecast", "variance analysis", "annual budget"])
        self.assertEqual((v["score"], v["ev"]), (88, "grounded"))
        self.assertEqual(self.tier(v)[1], "A")

    def test_verdicts_cached_before_the_grade_existed(self):
        g = lambda why: R.evidence_of({"by": "model", "why": why})
        self.assertEqual(g("Strong match: forecasting, budgeting, variance analysis."), "grounded")
        self.assertEqual(g("Strong match: forecasting. Gap: mostly close."), "partial")
        self.assertEqual(g("Reviewed: little planning or finance work."), "unsupported")
        self.assertEqual(g("Strong match: . (Model's evidence not found in the posting.)"), "unsupported")
        self.assertEqual(R.evidence_of(R.rules("FP&A Analyst", "", "strong")), "title_only")
        self.assertEqual(R.evidence_of(R.rules("FP&A Analyst", DUTIES, "strong")), "rules")


class ConnectTimeCheck(unittest.TestCase):
    def test_a_request_that_lands_on_a_local_address_is_refused_after_dns(self):
        # ats.http itself does no name check: only the connected socket's address can refuse this. That is what makes a DNS
        # answer that changes after an earlier lookup (rebinding) harmless.
        import http.server
        import threading
        srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}), ats._SafeHTTP, ats._SafeHTTPS, ats._SafeRedirect)
        old = ats._PROXIED
        ats._PROXIED = lambda: False
        try:
            with self.assertRaises(ats.UnsafeURL):
                ats.http(f"http://127.0.0.1:{srv.server_port}/", tries=1, want_json=False, opener=op.open)
        finally:
            ats._PROXIED = old
            srv.server_close()

    def test_every_request_uses_the_checked_opener_by_default(self):
        names = {type(h).__name__ for h in ats.SAFE_OPENER.handlers}
        self.assertTrue({"_SafeHTTP", "_SafeHTTPS", "_SafeRedirect"} <= names)
        self.assertNotIn("HTTPHandler", names, "the unchecked default handler must not also be installed")

    def test_dns_failure_fails_closed(self):
        with self.assertRaises(ats.UnsafeURL):
            ats.public_url("https://no-such-host.invalid/")

    def test_rippling_page_url_is_built_not_taken_from_the_reply(self):
        seen = []
        lst = {"items": [{"id": "77", "name": "FP&A Manager", "url": "http://169.254.169.254/latest/",
                          "locations": [{"city": "New York", "stateCode": "NY"}]}], "totalPages": 1}

        def get(url, **k):
            seen.append(url)
            if "pageSize" in url:
                return lst
            raise ats.HttpError(404, url)
        ats.rippling("acme", lambda j: True, get)
        self.assertEqual(seen[-1], "https://ats.rippling.com/acme/jobs/77")


class ManyOpeningsIsNotAMarketplace(unittest.TestCase):
    def copies(self, same_ad, trust="consistent", ids="lever:x"):
        base = "Gather requirements from program stakeholders and document business processes for a federal client. " * 4
        return [{"company": "CGS Federal", "title": "Business Analyst", "id": f"{ids}{i}", "trust": trust,
                 "description": base if same_ad else base + f" Client {i}: a different engagement with its own duties."}
                for i in range(10)]

    def test_one_ad_repeated_on_its_own_board_is_the_employer(self):
        rows = self.copies(True)
        employer.annotate(rows)
        self.assertEqual(rows[0]["employer_type"], "direct", "was marketplace: ten cities of one opening")
        self.assertIn("one ad posted many times", rows[0]["employer_type_why"])

    def test_one_title_with_different_ads_is_still_the_agency_pattern(self):
        rows = self.copies(False)
        employer.annotate(rows)
        self.assertEqual(rows[0]["employer_type"], "marketplace")

    def test_copies_on_job_sites_still_count(self):
        rows = self.copies(True, trust="unverified", ids="in-")
        employer.annotate(rows)
        self.assertEqual(rows[0]["employer_type"], "marketplace")


class ModelOverloaded(unittest.TestCase):
    def test_a_503_is_retried_instead_of_ending_the_runs_reviews(self):
        import urllib.error
        calls, waits = [], []

        def post(url, body, token):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 503, "overloaded", {}, None)
            ids = re.findall(r'<job id="([^"]+)"', body["contents"][0]["parts"][0]["text"])
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"jobs": [
                {"id": i, "score": 80, "category": R.FPA, "strengths": ["monthly forecast", "variance analysis"]} for i in ids]})}]}}]}
        items = [{"id": f"j{i}", "title": "FP&A Analyst", "company": "Acme", "description": DUTIES} for i in range(3)]
        cache = {"jobs": {}}
        st = R.run_reviews(items, cache, "k", post, max_calls=5, today="2026-11-18", sleep=waits.append, per_request=8)
        self.assertEqual(st["reviewed"], 3, "was 0: the first 503 stopped the run")
        self.assertEqual(st["server_retries"], 1)

    def test_a_service_that_stays_down_still_stops(self):
        import urllib.error

        def post(url, body, token):
            raise urllib.error.HTTPError(url, 503, "overloaded", {}, None)
        items = [{"id": "j", "title": "FP&A Analyst", "company": "Acme", "description": DUTIES}]
        st = R.run_reviews(items, {"jobs": {}}, "k", post, max_calls=10, today="2026-11-18", sleep=lambda s: None)
        self.assertEqual((st["reviewed"], st["server_retries"], st["error"]), (0, R.SERVER_RETRIES, "HTTP 503"))


class ThirdReview(unittest.TestCase):
    """Third outside review (Nov 18): grounding, the classify job's failure and retry reporting, the A/B check, identity."""

    def test_the_title_is_not_evidence_of_the_duties(self):
        def post(url, body, token):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"jobs": [
                {"id": "j", "score": 85, "category": R.FPA, "strengths": ["financial analysis"], "gap": ""}]})}]}}]}
        out = R.review([("j", "Financial Analyst", "Acme", "Run the night shift schedule and receive deliveries. " * 5)],
                       "k", "m", post)
        self.assertEqual(out["j"]["ev"], "unsupported", "was grounded: 'financial analysis' matched the title")

    def test_sixty_percent_means_rounded_up(self):
        text = "prepare monthly reports for the team"
        self.assertFalse(R.grounded("prepare quarterly board decks", text), "1 of 3 content words: below 60%")
        self.assertFalse(R.grounded("monthly forecast", text), "1 of 2 is 50%: round() let it pass")
        self.assertTrue(R.grounded("monthly reports", text))
        self.assertFalse(R.grounded("the and with", text), "a phrase of filler words proves nothing")

    def test_rephrasings_of_one_duty_count_once(self):
        self.assertEqual(R.distinct(["variance analysis", "analysis of variances", "monthly forecast"]),
                         ["variance analysis", "monthly forecast"])

    def test_cached_grades_are_rechecked_against_the_posting_and_only_go_down(self):
        v = {"by": "model", "why": "Strong match: own the forecast, variance analysis.", "ev": "grounded"}
        self.assertEqual(R.evidence_of(v, "Prepare variance analysis and own the forecast each month."), "grounded")
        self.assertEqual(R.evidence_of(v, "Prepare variance analysis for the team."), "partial", "the posting changed")
        self.assertEqual(R.evidence_of(dict(v, ev="partial"), DUTIES + " own the forecast"), "partial", "never up")

    def test_a_crash_is_recorded_and_the_watchdog_shows_it_red(self):
        import shutil
        import tempfile
        import watchdog
        d = tempfile.mkdtemp()
        try:
            json.dump({"ran_at": "2026-11-18T20:00:00+00:00"}, open(os.path.join(d, "feed_status.json"), "w"))
            try:
                raise KeyError("fit_score")
            except KeyError as e:
                self.assertTrue(R.record_failure(d, e))
            st = json.load(open(os.path.join(d, "feed_status.json")))
            self.assertEqual(st["classifier"]["status"], "failed")
            self.assertFalse(st["complete"]["classifier"])
            self.assertIn("crashed: KeyError", st["classifier"]["error"])
            src = open(os.path.join(ROOT, "watchdog.py"), encoding="utf-8").read()
            self.assertIn('"classifier-crash"', src)
            red = [f for f in watchdog.check(dict(st, direct={}), [], datetime_now()) if f["key"] == "classifier-crash"]
            self.assertEqual(red[0]["level"], "red")
        finally:
            shutil.rmtree(d)

    def test_one_retry_budget_for_both_stages_and_recovery_clears_the_error(self):
        import csv
        import shutil
        import tempfile
        import scrape
        import test_v81 as T
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "jobs.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=scrape.COLS, extrasaction="ignore")
                w.writeheader()
                w.writerows([T.row("indeed:1", title="Financial Analyst")])
            sample = [dict(T.row("greenhouse:50", title="Revenue Operations Analyst"), _drop="few planning duties")]
            held = [dict(T.row("greenhouse:1"), held_since="2026-11-17")]
            json.dump({"review_ids": [], "borderline": held, "audit": sample}, open(os.path.join(d, "review.json"), "w"))
            json.dump({"ran_at": "x"}, open(os.path.join(d, "feed_status.json"), "w"))
            cpath = os.path.join(d, "c.json")
            json.dump({"v": 1, "jobs": {}}, open(cpath, "w"))
            calls, ok = [], T.fake_post({}, [])
            import urllib.error

            def post(url, body, token, timeout=60):
                calls.append(1)
                if len(calls) in (1, 3):                  # a 503 in the sample stage, another in the main stage
                    raise urllib.error.HTTPError(url, 503, "overloaded", {}, None)
                return ok(url, body, token)
            R.time.sleep = lambda s: None
            st = R.main(d, cpath, post, token="tok", today="2026-11-18",
                        now=__import__("datetime").datetime(2026, 11, 18, 20, 0, tzinfo=__import__("datetime").timezone.utc))
            self.assertEqual(st["server_retries"], 2, "both stages' retries, from one budget")
            self.assertEqual(st["recovered"], 2)
            self.assertEqual(st["error"], "", "recovered: the 503 is not the run's state")
            self.assertEqual(st["status"], "complete")
            saved = json.load(open(os.path.join(d, "classify_cache.json")))
            self.assertNotIn("server_retries", saved.get("usage", {}), "the per-job counter is never saved")
            self.assertTrue(json.load(open(os.path.join(d, "feed_status.json")))["complete"]["classifier"])
        finally:
            shutil.rmtree(d)

    def test_rules_ranked_a_b_jobs_are_read_by_the_model(self):
        import csv
        import shutil
        import tempfile
        import scrape
        import test_v81 as T
        d = tempfile.mkdtemp()
        try:
            rows = [dict(T.row(f"greenhouse:{i}", title="FP&A Analyst"), priority="A", priority_score=str(90 - i), fit_by="rules",
                         fit_score="80") for i in range(3)]
            with open(os.path.join(d, "jobs.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=scrape.COLS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            json.dump({"review_ids": [], "borderline": [], "audit": []}, open(os.path.join(d, "review.json"), "w"))
            json.dump({"ran_at": "x"}, open(os.path.join(d, "feed_status.json"), "w"))
            cpath = os.path.join(d, "c.json")
            json.dump({"v": 1, "jobs": {}}, open(cpath, "w"))
            calls = []
            R.time.sleep = lambda s: None
            st = R.main(d, cpath, T.fake_post({"greenhouse:0": T.verdict(90)}, calls), token="tok", today="2026-11-18",
                        now=__import__("datetime").datetime(2026, 11, 18, 20, 0, tzinfo=__import__("datetime").timezone.utc))
            ab = st["ab_check"]
            self.assertEqual((ab["sampled"], ab["reviewed"]), (3, 3))
            self.assertEqual(ab["left_ab"], 2, "the two the model scored 30 (accounting) leave A/B")
            out = {r["id"]: r for r in csv.DictReader(open(os.path.join(d, "jobs.csv")))}
            self.assertEqual(out["greenhouse:1"]["fit_by"], "model")
        finally:
            shutil.rmtree(d)

    def test_a_board_slug_becoming_the_employers_name_is_not_a_reused_id(self):
        import audit
        prev = [{"id": "rippling:85e2", "company": "pronghorn-careers", "title": "Regional FP&A Analyst",
                 "job_url": "https://ats.rippling.com/pronghorn-careers/jobs/85e2"}]
        now = [dict(prev[0], company="Pronghorn Initiative Holdings LLC")]
        items = []
        audit.identity_checks(prev, now, items)
        self.assertFalse([i for i in items if "different employer" in i.get("title", "")])
        items = []
        audit.identity_checks(prev, [dict(prev[0], company="Northwind Traders")], items)
        self.assertTrue([i for i in items if "different employer" in i.get("title", "")], "a real change is still flagged")

    def test_a_match_long_ago_no_longer_outranks_unproven_boards(self):
        import discover as D
        old = {"last_hit": "2026-09-01", "hits": 4, "nyc": 3}
        fresh = {"hits": 0, "nyc": 40}
        self.assertLess(D.priority(fresh, "2026-11-18", 0.02), D.priority(old, "2026-11-18", 0.02))
        recent = {"last_hit": "2026-11-10", "hits": 1, "nyc": 1}
        self.assertLess(D.priority(recent, "2026-11-18", 0.02), D.priority(fresh, "2026-11-18", 0.02))

    def test_read_age_reports_stale_relevant_boards(self):
        import discover as D
        db = {"boards": {"ashby:a": {"nyc": 3, "ok": "2026-11-10"}, "ashby:b": {"nyc": 1, "ok": "2026-11-18"},
                         "ashby:c": {"nyc": 0, "ok": "2026-10-01"}}}
        r = D.read_age(db, "2026-11-18")["ashby"]
        self.assertEqual((r["boards"], r["max"], r["over_4_days"], r["oldest"]), (2, 8, 1, "ashby:a"))


def datetime_now():
    from datetime import datetime, timezone
    return datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)


class LevelGate(unittest.TestCase):
    """Nov 18: the candidate wants analyst to senior analyst only. Manager level and up is not published, read or searched."""

    def test_levels(self):
        import target
        up = ["FP&A Manager", "Senior Manager, Strategic Finance", "Director of FP&A", "AVP, Financial Planning",
              "Assistant Vice President - FP&A", "FP&A Supervisor", "Business Execution Finance - C14 - NEW YORK",
              "Head of Finance", "VP, Corporate Development"]
        ok = ["Senior Financial Analyst", "FP&A Analyst II", "Associate, FP&A", "Lead Financial Analyst - FP&A",
              "Business Execution Finance - C12 - NEW YORK", "Financial Analyst"]
        for t in up:
            self.assertTrue(target.off({"title": t, "description": DUTIES}), t)
        for t in ok:
            self.assertFalse(target.off({"title": t, "description": DUTIES}), t)
        self.assertTrue(target.off({"title": "Strategic Finance Lead", "description": DUTIES + " Manage a team of 3 analysts."}),
                        "a Lead with people to manage")

    def test_boards_never_fetch_manager_titles(self):
        import discover as D
        for t in ("FP&A Manager", "Manager, Financial Planning & Analysis", "Senior Manager FP&A", "Team Lead, Finance"):
            self.assertEqual(D.title_kind(t), "", t)
        self.assertEqual(D.title_kind("Senior Financial Analyst"), "strong")

    def test_last_feeds_manager_rows_leave_without_being_restored(self):
        import csv
        import shutil
        import tempfile
        import scrape
        import test_scrape as TS
        from datetime import datetime
        d = tempfile.mkdtemp()
        try:
            out, st = os.path.join(d, "jobs.csv"), os.path.join(d, "feed_status.json")
            prev = [dict({c: "" for c in scrape.COLS}, id=f"in-{i}", site="indeed", title="FP&A Manager", company=f"Co{i}",
                         location="New York, NY", job_url=f"https://indeed.example/{i}", description=DUTIES,
                         first_seen="2026-11-17", last_seen="2026-11-17", date_posted="2026-11-17") for i in range(30)]
            prev.append(dict(prev[0], id="in-99", title="Senior Financial Analyst", company="Keep Co",
                             job_url="https://indeed.example/99"))
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=scrape.COLS)
                w.writeheader()
                w.writerows(prev)
            json.dump([{"rows_by_site": {"indeed": 31}}] * 4, open(os.path.join(d, "audit_log.json"), "w"))
            json.dump({"v": 1, "run": 5, "searches": {}, "sites": {"glassdoor": {"state": "paused", "since": "2026-11-01",
                                                                                  "zero_runs": 24}}},
                      open(os.path.join(d, "search_stats.json"), "w"))
            old = scrape.SEARCHES
            scrape.SEARCHES = [("indeed", "fp&a", 5), ("glassdoor", "fp&a", 5)]
            try:
                fresh = [TS.job(200, "indeed", "Director, FP&A", "Big Co", desc=DUTIES),
                         TS.job(201, "indeed", "FP&A Analyst", "Small Co", desc=DUTIES)]
                scrape.main(scrape=TS.fake({("indeed", "fp&a"): fresh}), out=out, status_path=st,
                            now=datetime.fromisoformat("2026-11-18T12:00:00+00:00"))
            finally:
                scrape.SEARCHES = old
            rows = TS.read(out)
            titles = sorted(r["title"] for r in rows)
            self.assertEqual(titles, ["FP&A Analyst", "Senior Financial Analyst"])
            status = json.load(open(st))
            self.assertNotEqual(status["audit"]["level"], "block", "30 indeed rows left on purpose are not 'lost'")
            self.assertEqual(status["target"]["gate"]["left_out"], 31)
            self.assertEqual(status["target"]["violations"], 0)
            self.assertFalse([i for i in status["audit"]["items"] if i.get("key", "").startswith("coverage-rows_by_site")
                              or "against a usual" in i.get("title", "")],
                             "rows the gate left out still count as brought by their source")
            self.assertEqual(status["paused_probes_empty"], ["glassdoor"])
            self.assertEqual(status["empty_sites"], [])
            self.assertFalse(status["partial"], "a paused site's empty daily probe is not a partial run")
        finally:
            shutil.rmtree(d)

    def test_manager_searches_are_retired_not_run(self):
        import scheduler as S
        state = {"v": 1, "run": 0, "sites": {}, "searches": {
            "indeed|finance and strategy manager": {"site": "indeed", "term": "finance and strategy manager", "n": 30, "src": "pool",
                                                    "added": "2026-11-02", "runs_total": 40, "days": {}, "state": "active"}}}
        searches, why = S.plan(state, [("indeed", "fp&a", 30)], S.POOL, "2026-11-18")
        self.assertNotIn("finance and strategy manager", [t for _, t, _ in searches])
        self.assertTrue(why["indeed|finance and strategy manager"].startswith("retired"))
        self.assertIn("indeed|finance and strategy manager", state["searches"], "its history is kept")


if __name__ == "__main__":
    unittest.main()
