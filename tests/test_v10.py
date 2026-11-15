"""Offline tests for v10 (Nov 14 2026): fair search credit, credit from the final verdict, mined phrases, employer type, source
confirmation, quality-aware cap, priority caps, versioned verdicts and checked evidence."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import audit  # noqa: E402
import employer  # noqa: E402
import freshness  # noqa: E402
import priority  # noqa: E402
import relevance as R  # noqa: E402
import scheduler as S  # noqa: E402
import scrape  # noqa: E402

DUTIES = ("Own the monthly forecast, annual budget and variance analysis versus plan. Build driver-based financial models, "
          "partner with business leaders on KPI reporting and quarterly business reviews. " * 3)
ACCT = "Prepare journal entries, reconciliations, accruals and the month-end close under GAAP, AP/AR and audit support. " * 3


class DF:
    def __init__(self, recs):
        self.recs = recs

    def to_dict(self, orient):
        return self.recs


def rec(n, title="FP&A Analyst", company="Acme", desc=DUTIES, site="indeed", url=None):
    return {"id": f"in-{n}", "site": site, "title": title, "company": company, "location": "New York, NY", "is_remote": False,
            "date_posted": "2026-11-13", "job_url": url or f"https://www.indeed.com/viewjob?jk={n}", "job_url_direct": "",
            "description": desc}


def fake_scrape(table):
    def scrape_jobs(**kw):
        return DF(table.get(kw.get("search_term") or kw.get("google_search_term"), []))
    return scrape_jobs


class FairCredit(unittest.TestCase):
    def run_order(self, order):
        table = {"a": [rec(1), rec(2)], "b": [rec(1)], "c": [rec(3, title="Staff Accountant", desc=ACCT)]}
        searches = [("indeed", t, 10) for t in order]
        _, st = scrape.collect(fake_scrape(table), searches, pause=(0, 0), sleep=lambda s: None, previous=[], pending={})
        return {s["search"]: s for s in st}

    def test_shared_job_is_split_and_order_does_not_matter(self):
        for order in (["a", "b", "c"], ["b", "a", "c"], ["c", "b", "a"]):
            st = self.run_order(order)
            self.assertEqual(st["a"]["qnew"], 1.5, order)       # job 1 shared (0.5) + job 2 alone (1)
            self.assertEqual(st["b"]["qnew"], 0.5, order)
            self.assertEqual(st["a"]["excl"], 1)
            self.assertEqual(st["b"]["excl"], 0)
            self.assertEqual(st["b"]["qualified"], 1, "a duplicate still counts as a qualified result for that search")

    def test_known_job_earns_nothing(self):
        table = {"a": [rec(1)]}
        prev = [dict(rec(1), first_seen="2026-11-12", last_seen="2026-11-13", source_ids="in-1")]
        _, st = scrape.collect(fake_scrape(table), [("indeed", "a", 10)], pause=(0, 0), sleep=lambda s: None, previous=prev)
        self.assertEqual(st[0]["qnew"], 0)
        self.assertEqual(st[0]["qualified"], 1)

    def test_same_opening_from_two_sites_shares_one_credit(self):
        table = {"a": [rec(1)], "b": [dict(rec(9, site="linkedin", url="https://www.linkedin.com/jobs/view/9"), id="li-9")]}
        fresh, st = scrape.collect(fake_scrape(table), [("indeed", "a", 10), ("linkedin", "b", 10)], pause=(0, 0),
                                   sleep=lambda s: None, previous=[])
        self.assertEqual([s["qnew"] for s in st], [0.5, 0.5])
        self.assertEqual(len(fresh), 2, "both copies still go to merge(), which folds them and keeps also_on")

    def test_cached_model_verdict_counts(self):
        r = rec(5, title="Business Analyst", desc="Coordinate projects and stakeholders. " * 10)
        cache = {"jobs": {"in-5": {"score": 80, "category": R.FPA, "why": "", "by": "model",
                                   "h": R.digest(r["title"], scrape.clean(r["description"]))}}}
        _, st = scrape.collect(fake_scrape({"a": [r]}), [("indeed", "a", 10)], pause=(0, 0), sleep=lambda s: None,
                               previous=[], cache=cache)
        self.assertEqual(st[0]["qnew"], 1)


class Settle(unittest.TestCase):
    def state(self):
        s = {"run": 1, "searches": {}, "sites": {}}
        for k in ("indeed|a", "indeed|b"):
            site, term = k.split("|")
            s["searches"][k] = {"site": site, "term": term, "n": 10, "src": "base", "runs_total": 1,
                                "days": {"2026-11-13": [1, 5, 5, 0, 0, 0, 0]}}
        return s

    def test_rescue_and_demote_move_credit(self):
        s = self.state()
        S.remember(s, {"in-1": [["indeed|a", "indeed|b"], 0], "in-2": [["indeed|a"], 1], "in-3": [["indeed|b"], 0]}, "2026-11-13")
        s["searches"]["indeed|a"]["days"]["2026-11-13"][4] = 1          # in-2's provisional credit
        s["searches"]["indeed|a"]["days"]["2026-11-13"][3] = 1
        pub = [{"id": "greenhouse:9", "source_ids": "greenhouse:9;in-1", "fit_score": "80", "fit_by": "model", "location": "New York, NY"},
               {"id": "in-2", "fit_score": "30", "fit_by": "model", "location": "New York, NY"},
               {"id": "in-3", "fit_score": "40", "fit_by": "rules", "location": "New York, NY"}]
        out = S.settle(s, pub, "2026-11-14", scrape.final_qualified)
        self.assertEqual((out["rescued"], out["demoted"], out["waiting"]), (1, 1, 1))
        self.assertEqual(s["searches"]["indeed|a"]["days"]["2026-11-13"][4], 0.5)   # +0.5 rescued, -1 demoted, from 1
        self.assertEqual(s["searches"]["indeed|b"]["days"]["2026-11-13"][4], 0.5)
        self.assertIn("in-3", s["pending"], "not reviewed yet: keeps waiting")

    def test_pending_expires(self):
        s = self.state()
        S.remember(s, {"in-1": [["indeed|a"], 0]}, "2026-11-01")
        S.remember(s, {}, "2026-11-14")
        self.assertEqual(s["pending"], {})

    def test_migration_retrials_paused_searches(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.json")
        json.dump({"v": 1, "run": 9, "sites": {}, "searches": {"indeed|wfp": {"site": "indeed", "term": "wfp", "state": "paused",
                   "runs_total": 40, "days": {}}, "indeed|fpa": {"site": "indeed", "term": "fpa", "state": "active", "runs_total": 40, "days": {}}}},
                  open(path, "w"))
        st = S.load(path)
        self.assertEqual((st["searches"]["indeed|wfp"]["runs_total"], st["searches"]["indeed|wfp"]["retrial"]), (0, "paused"))
        self.assertEqual(st["searches"]["indeed|fpa"]["runs_total"], 40)
        S.save(st, path)
        self.assertEqual(S.load(path)["searches"]["indeed|wfp"]["runs_total"], 0, "migrates once")

    def test_old_six_field_days_still_read(self):
        e = {"days": {"2026-11-13": [1, 2, 3, 4, 5, 0]}}
        self.assertEqual(S._window(e, "2026-11-14", 7)["qnew"], 5)


class Mined(unittest.TestCase):
    def rows(self, title, n, fit="80", cat=R.FPA):
        return [{"id": f"x{title}{i}", "title": title, "company": f"Co{i}", "fit_score": fit, "fit_category": cat,
                 "location": "New York, NY"} for i in range(n)]

    def test_proposes_uncovered_phrase_only(self):
        rows = self.rows("Finance & Strategy Analyst", 4) + self.rows("Revenue Finance Analyst", 4) + self.rows("Senior FP&A Analyst", 5) + self.rows("Tax Strategy Analyst", 5)
        got = scrape.mined_pool(rows, {"searches": {}})
        terms = [t for _, t, _, _ in got]
        self.assertIn("finance and strategy analyst", terms)
        self.assertNotIn("revenue finance analyst", terms, "the 'revenue analyst' trial already matches every word")
        self.assertNotIn("fp&a analyst", terms, "an existing search already matches those titles")
        self.assertFalse(any("tax" in t for t in terms))

    def test_needs_precision_and_employers(self):
        rows = self.rows("Pricing Strategy Analyst", 2)
        self.assertEqual(scrape.mined_pool(rows, {"searches": {}}), [])
        rows = self.rows("Pricing Strategy Analyst", 3) + self.rows("Pricing Strategy Analyst", 5, fit="20")
        self.assertEqual(scrape.mined_pool(rows, {"searches": {}}), [], "most feed jobs with the phrase don't qualify")

    def test_scheduler_caps_mined_trials(self):
        st = {"run": 0, "searches": {}, "sites": {}}
        pool = [("indeed", f"phrase {i}", 30, "mined") for i in range(6)]
        S.POOL_ADMIT, keep = 10, S.POOL_ADMIT
        try:
            _, dec = S.plan(st, [("indeed", "base", 10)], pool, "2026-11-14")
        finally:
            S.POOL_ADMIT = keep
        self.assertEqual(sum(1 for v in dec.values() if "mined" in v), S.MINED_ACTIVE)

    def test_title_core(self):
        self.assertEqual(scrape.title_core("Sr. Revenue Finance Analyst II, Ads (Hybrid)"), "revenue finance analyst")


class EmployerType(unittest.TestCase):
    def c(self, company, desc="", **kw):
        return employer.classify(dict({"company": company, "description": desc, "id": "in-1"}, **kw))[0]

    def test_names(self):
        for name, want in (("Nextera Search", "search_firm"), ("Northbound Executive Search", "search_firm"), ("Robert Half", "staffing"),
                           ("Insite Recruitment", "staffing"), ("jobgether", "marketplace"), ("Confidential", "undisclosed"),
                           ("", "undisclosed"), ("Acme Corp", "unknown")):
            self.assertEqual(self.c(name), want, name)

    def test_no_false_positives(self):
        for name in ("Atrium Health", "Monster Energy", "Crossover Health", "Market Research Group", "Temple University",
                     "Center for Alternative Sentencing & Employment Services"):
            self.assertEqual(self.c(name), "unknown", name)
        for text in ("As investors on behalf of our clients, it is our role to find investment solutions.",
                     "You will prepare analyses on behalf of a client in litigation and support confidential client matters.",
                     "Partner with our clients to meet their needs."):
            self.assertEqual(self.c("Brattle", text), "unknown", text)

    def test_recruiter_phrasing(self):
        self.assertEqual(self.c("Alldus", "Our client, an AI-driven company, is hiring a Founding Analyst."), "staffing")
        self.assertEqual(self.c("Pavago", "One of our clients is seeking an FP&A Analyst."), "staffing")

    def test_board_and_mass(self):
        self.assertEqual(self.c("Acme", id="greenhouse:1", trust="verified"), "direct")
        rows = [{"company": "BigPost", "title": "Remote Advisor", "id": f"in-{i}"} for i in range(9)]
        employer.annotate(rows)
        self.assertEqual(rows[0]["employer_type"], "marketplace")


class Priority(unittest.TestCase):
    BASE = {"fit_score": "90", "freshness": "fresh", "trust": "verified", "region": "NYC", "seniority": "analyst", "status": "new"}

    def tier(self, **kw):
        return priority.score(dict(self.BASE, **kw))[1]

    def test_base_is_a(self):
        self.assertEqual(self.tier(), "A")

    def test_caps(self):
        self.assertEqual(self.tier(fit_category=R.ACCT), "C")
        self.assertEqual(self.tier(employer_type="staffing"), "C")
        self.assertEqual(self.tier(employer_type="undisclosed"), "C")
        self.assertEqual(self.tier(confirm="unconfirmed"), "C")
        self.assertEqual(self.tier(confirm="not_due"), "A")
        self.assertEqual(self.tier(confirm="window"), "A")


class Confirm(unittest.TestCase):
    def test_states(self):
        t = "2026-11-14"
        self.assertEqual(freshness.confirm({"last_seen": t}, t), "current")
        self.assertEqual(freshness.confirm({"id": "workday:1", "last_seen": "2026-11-12", "_confirm": "not_due"}, t), "not_due")
        self.assertEqual(freshness.confirm({"id": "in-1", "last_seen": "2026-11-12", "date_posted": "2026-11-05"}, t), "window")
        self.assertEqual(freshness.confirm({"id": "greenhouse:1", "last_seen": "2026-11-13"}, t), "recent")
        self.assertEqual(freshness.confirm({"id": "greenhouse:1", "last_seen": "2026-11-11"}, t), "unconfirmed")

    def test_audit_only_warns_on_real_gaps(self):
        rows = [{"last_seen": "2026-11-12", "confirm": "not_due"}] * 50 + [{"last_seen": "2026-11-14"}] * 10
        items = []
        audit.unconfirmed(rows, "2026-11-14", items)
        self.assertEqual(items[0]["level"], "info")
        rows = [{"last_seen": "2026-11-11", "confirm": "unconfirmed"}] * 50 + [{"last_seen": "2026-11-14"}] * 10
        items = []
        audit.unconfirmed(rows, "2026-11-14", items)
        self.assertEqual(items[0]["level"], "warn")


class Cap(unittest.TestCase):
    def test_qualified_beat_newer_misfits_and_employers_share(self):
        rows = [{"id": f"greenhouse:a{i}", "title": "Staff Accountant", "company": "Ledger", "description": ACCT,
                 "date_posted": "2026-11-14"} for i in range(5)]
        rows += [{"id": f"greenhouse:b{i}", "title": "FP&A Analyst", "company": "Big" if i < 4 else f"Co{i}", "description": DUTIES,
                  "date_posted": "2026-10-01"} for i in range(6)]
        keep = scrape.CAP_PER_EMPLOYER
        scrape.CAP_PER_EMPLOYER = 2
        try:
            ranked = scrape.cap_rank(rows)
        finally:
            scrape.CAP_PER_EMPLOYER = keep
        top = [r["id"] for r in ranked[:4]]
        self.assertTrue(all(i.startswith("greenhouse:b") for i in top))
        self.assertEqual(sum(1 for r in ranked[:4] if r["company"] == "Big"), 2, "diversity: other employers before Big's 3rd")
        rep = scrape.cap_report(ranked, 6)
        self.assertEqual((rep["trimmed"], rep["trimmed_qualified"]), (5, 0))


class Evidence(unittest.TestCase):
    def test_grounded(self):
        self.assertTrue(R.grounded("monthly forecasting", "You will forecast revenue each month"))
        self.assertFalse(R.grounded("M&A due diligence", "Own the budget and the forecast"))

    def test_review_drops_invented_strengths(self):
        def post(url, body, token):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"jobs": [
                {"id": "j1", "score": 80, "category": R.FPA, "strengths": ["variance analysis", "M&A due diligence"], "gap": ""},
                {"id": "j2", "score": 70, "category": R.FPA, "strengths": ["capital markets syndication"], "gap": ""}]})}]}}]}
        out = R.review([("j1", "FP&A Analyst", "Acme", DUTIES), ("j2", "FP&A Analyst", "Acme", DUTIES)], "k", "m", post)
        self.assertIn("variance analysis", out["j1"]["why"])
        self.assertNotIn("due diligence", out["j1"]["why"])
        self.assertIn("evidence not found", out["j2"]["why"])
        self.assertEqual(out["j1"]["score"], 80, "the check changes the words, never the score")
        self.assertEqual(out["j1"]["pv"], R.PROMPT_VERSION)

    def test_stale_versions(self):
        self.assertFalse(R.stale({"by": "model"}) if R.PROMPT_VERSION == 1 else False)
        keep = R.PROMPT_VERSION
        R.PROMPT_VERSION = keep + 1
        try:
            self.assertTrue(R.stale({"by": "model", "pv": keep}))
            self.assertFalse(R.stale({"by": "model", "pv": keep + 1}))
        finally:
            R.PROMPT_VERSION = keep


class SweepFairness(unittest.TestCase):
    def test_workday_is_read_even_when_fast_boards_use_the_budget(self):
        import threading
        import ats
        import discover as D
        boards = {f"greenhouse:g{i}": {"added": "2026-11-01", "company": f"G{i}"} for i in range(40)}
        boards.update({f"workday:w{i}|wd1|Site": {"added": "2026-11-01", "company": f"W{i}"} for i in range(2)})
        db = {"boards": boards, "run": 0}
        keep = ats.fetch_board

        def fetch(b, want, get=None):
            threading.Event().wait(0.1 if b.startswith("greenhouse:") else 0.01)   # other tests stub time.sleep
            return ats.Jobs([])
        ats.fetch_board = fetch
        try:
            stats = {}
            D.sweep(db, "2026-11-14", stats, get=None, seconds=0.25)
        finally:
            ats.fetch_board = keep
        h = stats["adapter_health"]
        self.assertEqual(h["workday"]["read"], 2, "Workday no longer waits behind the fast group")
        self.assertGreater(h["greenhouse"]["late"], 0, "running out of time is counted, not silent")
        self.assertIn("ran out of time", h["greenhouse"]["why"])


class DescriptionFit(unittest.TestCase):
    DUTIES = "Own the monthly forecast and annual budget; build driver-based models; partner with leaders on KPIs.\n" * 45
    BOILER = ("Benefits\nWe offer medical, dental, 401(k), paid time off, parental leave and tuition support.\n" * 8 +
              "We are an equal opportunity employer and consider applicants without regard to national origin or disability.\n" * 6)
    REQS = "Qualifications\n- Bachelor's degree in Finance\n- 3-5 years of FP&A experience required\nSalary range $95,000-$115,000. Hybrid, 3 days a week.\n"

    def test_requirements_kept_boilerplate_dropped(self):
        import textfit
        for sep in ("\n", "\\n"):
            t = (self.DUTIES + self.BOILER * 2 + self.REQS).replace("\n", sep)
            f = textfit.fit(t, 6000)
            self.assertLessEqual(len(f), 6000)
            self.assertIn("3-5 years", f)
            self.assertIn("$95,000", f)
            self.assertNotIn("without regard to", f[4000:])
            self.assertIn("[...]", f)

    def test_keys_that_must_not_change(self):
        import history
        import textfit
        t = self.DUTIES + self.BOILER * 2 + self.REQS
        f = textfit.fit(t, 6000)
        self.assertEqual(f[:4000], t[:4000])
        self.assertEqual(R.digest("t", f), R.digest("t", t[:6000]), "cached model verdicts stay valid")
        self.assertEqual(R.norm_key("t", f), R.norm_key("t", t[:6000]))
        self.assertEqual(history._h(f), history._h(t[:6000]), "no 'description edited' events on deploy")
        self.assertEqual(textfit.fit("short", 6000), "short")


class BoardsDue(unittest.TestCase):
    def test_compares_with_boards_due(self):
        log = [{"boards": {"boards read": 1800}}] * 5
        items = []
        audit.coverage(log, {"boards": {"boards read": 532}, "boards_due": 534}, items)
        self.assertEqual(items, [], "532 of 534 due is fine whatever the usual total")
        audit.coverage(log, {"boards": {"boards read": 100}, "boards_due": 534}, items)
        self.assertEqual(items[0]["key"] if "key" in items[0] else items[0].get("id", "coverage-boards-due"), items[0].get("key", items[0].get("id")))
        self.assertIn("Read 100 of the 534", items[0]["title"])


class TrustTld(unittest.TestCase):
    def test_name_on_unsettled_tld_is_not_proof(self):
        import trust
        self.assertEqual(trust.domain_verdict("careers.acme.com", "Acme", set(), {}, {})[0], "employer")
        self.assertEqual(trust.domain_verdict("acme.careers", "Acme", set(), {}, {})[0], "unknown")
        self.assertEqual(trust.domain_verdict("acme.group", "Acme", set(), {}, {})[0], "unknown")
        self.assertEqual(trust.domain_verdict("acme.group", "Acme", {"acme.group"}, {}, {})[0], "employer", "the employer's site says so")
        self.assertEqual(trust.domain_verdict("acme.xyz", "Acme", set(), {}, {})[0], "suspicious")


class PayFromText(unittest.TestCase):
    def p(self, text):
        import reqs
        return reqs.pay_from_text(text)

    def test_stated_ranges(self):
        self.assertEqual(self.p("The base salary range for this role is $100,000 to $125,000 USD."), ("100000", "125000"))
        self.assertEqual(self.p("Expected base salary: $175k-$225k."), ("175000", "225000"))
        self.assertEqual(self.p("Compensation: $28 - $35 per hour."), ("58200", "72800"))

    def test_new_york_range_wins_and_ambiguity_gives_none(self):
        t = "Pay range: Colorado $80,000 - $95,000. New York City pay range: $110,000 - $130,000."
        self.assertEqual(self.p(t), ("110000", "130000"))
        self.assertEqual(self.p("Salary range $80,000 - $95,000 or $110,000 - $130,000 depending on level."), ("", ""))
        self.assertEqual(self.p("California salary range $120,000 - $150,000."), ("", ""), "another state's range only: don't guess")

    def test_not_pay(self):
        self.assertEqual(self.p("You will manage a budget of $100,000 - $500,000 per project."), ("", ""))
        self.assertEqual(self.p("Deals of $10,000,000 to $50,000,000 in revenue."), ("", ""))
        self.assertEqual(self.p("No pay stated."), ("", ""))


class OneRowPerId(unittest.TestCase):
    def test_same_posting_with_a_new_apply_link_stays_one_row(self):
        old = dict(rec(1), job_url_direct="https://boards.greenhouse.io/acme/jobs/111", first_seen="2026-11-12",
                   last_seen="2026-11-13", source_ids="in-1")
        old = {c: scrape.clean(old.get(c, "")) for c in scrape.COLS}
        new = {c: scrape.clean(v) for c, v in dict(rec(1), job_url_direct="https://boards.greenhouse.io/acme/jobs/999").items()}
        out = scrape.merge([old], [new], "2026-11-14")
        self.assertEqual([r["id"] for r in out], ["in-1"], "it used to add a second in-1 row and the run's check failed")
        self.assertEqual(out[0]["last_seen"], "2026-11-14")

    def test_safety_net(self):
        rows, dup = scrape.unique_ids([{"id": "a", "source_ids": "a"}, {"id": "b"}, {"id": "a", "source_ids": "a;x"}])
        self.assertEqual([r["id"] for r in rows], ["a", "b"])
        self.assertEqual(rows[0]["source_ids"], "a;x")
        self.assertEqual(dup["count"], 1)


class Shadow(unittest.TestCase):
    def test_records_without_raising(self):
        import shadow
        rows = [{"job_url_direct": "https://ejcu.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1033/requisitions/preview/1000430"},
                {"job_url_direct": "https://JPMorganChase.contacthr.com/154174946"}]
        def get(url, data=None, **kw):
            if "myworkdayjobs" in url:
                return {"total": 40, "jobPostings": [{"title": "Senior Associate", "locationsText": "New York, NY"}]}
            if "recruitingCEJobRequisitions?" in url:
                self.assertIn("siteNumber=CX_1033", url)
                return {"items": [{"TotalJobsCount": 2, "requisitionList": [{"Id": "1", "Title": "FP&A Analyst", "PostedDate": "2026-11-10"},
                                                                          {"Id": "2", "Title": "Analyst"}]}]}
            return {"items": [{"ExternalDescriptionStr": "Own the forecast."}]}
        out = shadow.run(rows, get, lambda u: "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1")
        self.assertEqual((out["oracle"][0]["ok"], out["oracle"][0]["total"], out["oracle"][0]["detail_chars"] > 5), (True, 2, True))
        self.assertTrue(out["contacthr"][0]["oracle"])
        self.assertEqual((out["workday"][0]["total"], out["workday"][0]["nyc"]), (40, 1))
        def bad(url, *a, **kw):
            raise OSError("blocked")
        out = shadow.run(rows, bad, bad)
        self.assertFalse(out["oracle"][0]["ok"])
        self.assertIn("error", out["contacthr"][0])


if __name__ == "__main__":
    unittest.main()
