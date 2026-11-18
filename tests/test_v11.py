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


if __name__ == "__main__":
    unittest.main()
