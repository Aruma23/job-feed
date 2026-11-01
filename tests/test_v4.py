"""Offline tests for v4: location resolver, layered identity, relevance (rules + model review), search scheduler, discovery."""
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ats  # noqa: E402
import discover as D  # noqa: E402
import geo  # noqa: E402
import identity as I  # noqa: E402
import relevance as R  # noqa: E402
import scheduler as S  # noqa: E402
import scrape  # noqa: E402

scrape.random.uniform = lambda a, b: 0
scrape.time.sleep = lambda s: None
D.time.sleep = lambda s: None
ats.time.sleep = lambda s: None


class Geo(unittest.TestCase):
    CASES = {
        "Yonkers, NY, USA": "local", "White Plains, NY": "local", "Bronx, NY, USA": "local", "Bergen County, NJ, USA": "local",
        "Stamford, CT, us": "local", "Greenwich, CT": "local", "Westchester County, NY": "local", "Fairfield County, CT": "local",
        "Long Island City, NY, USA": "local", "Florham Park, NJ, USA": "local", "New York, NY 10001": "local",
        "US-NY-New York": "local", "NY - New York, 1407 Broadway Floor 12": "local", "United States-New York-New York": "local",
        "New York City Metropolitan Area": "local", "SF, SEA, CHI, NYC": "local", "Toronto GTA; New York; San Francisco": "local",
        "Albany, New York": "out", "Buffalo, New York, United States": "out", "Newark, DE": "out", "Madison, WI": "out",
        "Greenwich, London, UK": "out", "Danbury, CT": "out", "New York Mills, MN": "out", "San Francisco, CA": "out",
        "Remote - United States": "remote_us", "Remote, NY": "remote_us", "Remote; Remote": "remote_us",
        "Remote (United States | Canada); Remote": "remote_us", "Remote PA": "out", "Remote, Germany": "out",
        "REMOTE/TELETRAVAIL, ON, CAN": "out", "LatAm; Remote": "out", "Ontario, CA": "out",
        "2 Locations": "maybe", "Multiple Locations": "maybe", "New Jersey, United States": "maybe", "": "maybe",
    }

    def test_cases(self):
        for loc, want in self.CASES.items():
            self.assertEqual(geo.resolve(loc).status, want, loc)

    def test_remote_flag_and_iso_country(self):
        self.assertEqual(geo.resolve("Lyon, Auvergne-Rhone-Alpes, fr", True).status, "out", "SmartRecruiters remote job in France")
        self.assertEqual(geo.resolve("Kingsland, Georgia, us", True).status, "remote_us")

    def test_tiers_are_configurable(self):
        self.assertEqual(geo.resolve("Danbury, CT").region, "fairfield_outer")
        geo.ENABLED_TIERS.add("stretch")
        try:
            self.assertEqual(geo.resolve("Danbury, CT").status, "local")
        finally:
            geo.ENABLED_TIERS.discard("stretch")

    def test_description_settles_multiple_locations(self):
        self.assertEqual(geo.resolve("2 Locations", text="Hybrid from our New York City office").status, "local")

    def test_finder_hint_only_where_needed(self):
        p = geo.resolve("Yonkers, NY, USA")
        self.assertEqual(geo.finder_location("Yonkers, NY, USA", p), "Yonkers, NY, USA (New York metro: Westchester)")
        self.assertEqual(geo.finder_location("New York, NY", geo.resolve("New York, NY")), "New York, NY")
        self.assertEqual(scrape.HINT.sub("", "Yonkers, NY, USA (New York metro: Westchester)"), "Yonkers, NY, USA")

    def test_discover_uses_regions(self):
        j = {"title": "Financial Analyst", "location": "White Plains, NY", "description": "", "remote": False, "company": "Acme"}
        self.assertTrue(D.keep(j))
        self.assertTrue(D.want(dict(j, location="2 Locations")), "Workday multi-location: fetch the detail page")


def row(i, site, title, company, loc="New York, NY", direct="", desc="", url=None):
    return {"id": i, "site": site, "title": title, "company": company, "location": loc, "is_remote": "False", "date_posted": "2026-10-31",
            "job_url": url or f"https://{site}.example/{i}", "job_url_direct": direct, "min_amount": "", "max_amount": "", "interval": "",
            "description": desc}


class Identity(unittest.TestCase):
    def test_same_title_two_locations_stay_two(self):
        rows = scrape.merge([], [row("in-1", "indeed", "Financial Analyst", "Acme"), row("li-2", "linkedin", "Financial Analyst", "Acme", "Stamford, CT")], "2026-11-01")
        self.assertEqual(len(rows), 2)

    def test_same_opening_across_sites_merges_and_keeps_source_ids(self):
        rows = scrape.merge([], [row("in-1", "indeed", "Financial Analyst", "Acme Inc."),
                                 row("li-2", "linkedin", "Financial Analyst (Hybrid)", "ACME", "New York City Metropolitan Area")], "2026-11-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "in-1")
        self.assertEqual(rows[0]["source_ids"], "in-1;li-2")

    def test_different_requisitions_never_merge(self):
        a = row("in-1", "indeed", "Financial Analyst", "Acme", direct="https://acme.wd5.myworkdayjobs.com/Ext/job/NY/Financial-Analyst_R100")
        b = row("li-2", "linkedin", "Financial Analyst", "Acme", direct="https://acme.wd5.myworkdayjobs.com/Ext/job/NY/Financial-Analyst_R200")
        self.assertEqual(len(scrape.merge([], [a, b], "2026-11-01")), 2)

    def test_same_requisition_merges_even_with_different_titles(self):
        a = row("in-1", "indeed", "Financial Analyst", "Acme", direct="https://acme.wd5.myworkdayjobs.com/Ext/job/NY/Financial-Analyst_R100?source=Indeed")
        b = row("li-2", "linkedin", "Financial Analyst II - FP&A", "Acme Corp", direct="https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/NY/Financial-Analyst_R100/apply")
        self.assertEqual(len(scrape.merge([], [a, b], "2026-11-01")), 1)

    def test_two_postings_on_one_site_stay_two(self):
        rows = scrape.merge([], [row("in-1", "indeed", "Financial Analyst", "Acme"), row("in-2", "indeed", "Financial Analyst", "Acme")], "2026-11-01")
        self.assertEqual(len(rows), 2)

    def test_requisition_from_description(self):
        a = row("in-1", "indeed", "Financial Analyst", "Acme", desc="Requisition ID: 4471. Build forecasts.")
        b = row("li-2", "linkedin", "Financial Analyst", "Acme", desc="Req ID: 4472")
        self.assertEqual(len(scrape.merge([], [a, b], "2026-11-01")), 2)
        self.assertEqual(I.req_label(I.req_keys(a)), "desc:acme:4471")

    def test_canonical_url_drops_tracking_only(self):
        self.assertEqual(I.canon_url("https://www.Acme.com/careers/123/apply/?utm_source=x&gh_jid=9"), "acme.com/careers/123?gh_jid=9")
        self.assertEqual(I.canon_url("https://tnl2.jometer.com/v2/job?jz=5wq&utm_source=indeed"), "tnl2.jometer.com/v2/job?jz=5wq", "tracker links keep the job parameter")

    def test_shared_careers_page_is_not_an_identity(self):
        a = row("in-1", "indeed", "Financial Analyst", "Acme", direct="https://acme.com/careers")
        b = row("in-2", "indeed", "Budget Analyst", "Acme", direct="https://acme.com/careers")
        self.assertEqual(len(scrape.merge([], [a, b], "2026-11-01")), 2)

    def test_annotate_needs_matching_location(self):
        direct = [dict(row("greenhouse:1", "greenhouse", "Financial Analyst", "Acme", "Stamford, CT", url="https://job-boards.greenhouse.io/acme/jobs/1"), is_remote="False"),
                  dict(row("greenhouse:2", "greenhouse", "Financial Analyst", "Acme", "New York, NY", url="https://job-boards.greenhouse.io/acme/jobs/2"), is_remote="False")]
        agg = [row("in-1", "indeed", "Financial Analyst", "Acme", "New York, NY")]
        self.assertEqual(scrape.annotate(agg, direct), 1)
        self.assertEqual(agg[0]["job_url_direct"], "https://job-boards.greenhouse.io/acme/jobs/2")
        self.assertIn("greenhouse:2", agg[0]["source_ids"])


ACCT_DESC = ("Prepare journal entries and maintain the general ledger. Perform account reconciliations and support month-end close, "
             "accruals and audit requests. Some budgeting support.")
FPA_DESC = ("Own the annual operating plan and monthly forecast. Variance analysis vs. budget, P&L reviews with business partners, "
            "long-range plan and scenario analysis for the CFO.")


class Relevance(unittest.TestCase):
    def test_rules(self):
        a = R.rules("Financial Analyst", ACCT_DESC, "strong")
        self.assertEqual(a["category"], R.ACCT)
        self.assertTrue(a["review"], "an accounting-heavy 'Financial Analyst' goes to review")
        f = R.rules("Financial Analyst", FPA_DESC, "strong")
        self.assertGreaterEqual(f["score"], 85)
        self.assertEqual(f["category"], R.FPA)
        self.assertIn("forecasting", f["why"])
        w = R.rules("Analyst, Operations", "Build the headcount and capacity planning model for our contact center; staffing levels, service levels, volume forecasting.", "ambiguous")
        self.assertEqual(w["category"], R.WFP)

    def test_borderline_board_job_is_held_not_dropped(self):
        j = {"title": "Operations Associate", "location": "New York, NY", "remote": False, "company": "Acme", "description": "You will help with the budget."}
        self.assertEqual(D.judge(j), "borderline")
        self.assertEqual(D.judge(dict(j, description="Run the warehouse schedule.")), "drop")

    def fake_post(self, verdicts, calls):
        def post(url, body, token, timeout=60):
            calls.append(body)
            ids = [ln.split('"')[1] for ln in body["messages"][1]["content"].splitlines() if ln.startswith("<job id=")]
            return {"choices": [{"message": {"content": json.dumps({"jobs": [dict(verdicts[i], id=i) for i in ids if i in verdicts]})}}]}
        return post

    def test_review_parses_caches_and_respects_budget(self):
        items = [row(f"in-{i}", "indeed", "Analyst", "Acme", desc=FPA_DESC) for i in range(10)]
        verdicts = {f"in-{i}": {"score": 91, "category": "Commercial Finance / business partnering", "strengths": ["forecasting", "variance analysis"], "gap": "no long-range planning"} for i in range(10)}
        calls, cache = [], {"jobs": {}}
        st = R.run_reviews(items, cache, "tok", self.fake_post(verdicts, calls), max_calls=2, today="2026-11-01", sleep=lambda s: None)
        self.assertEqual((st["calls"], st["reviewed"], st["left"]), (2, 8, 2))
        v = R.cached(cache, items[0])
        self.assertEqual((v["score"], v["category"]), (91, R.COMM))
        self.assertEqual(v["why"], "Strong match: forecasting, variance analysis. Gap: no long-range planning.")
        self.assertIsNone(R.cached(cache, dict(items[0], description="changed")), "a changed description is reviewed again")

    def test_review_stops_on_rate_limit_and_without_token(self):
        def post(*a, **k):
            raise urllib.error.HTTPError("u", 429, "slow down", {}, io.BytesIO(b""))
        st = R.run_reviews([row("in-1", "indeed", "A", "B", desc=FPA_DESC)], {"jobs": {}}, "tok", post, sleep=lambda s: None)
        self.assertEqual((st["calls"], st["error"]), (0, "HTTP 429"))
        self.assertIn("no GITHUB_TOKEN", R.run_reviews([1], {"jobs": {}}, "")["error"])

    def test_classify_job_rescues_and_demotes(self):
        d = tempfile.mkdtemp()
        try:
            good = row("in-1", "indeed", "Financial Analyst", "Acme", desc="Prepare the monthly forecast and budget, with variance analysis. "
                       "Also journal entries, account reconciliations and month-end close tasks for the team.")
            held = dict(row("greenhouse:7", "greenhouse", "Operations Associate", "Beta", desc=FPA_DESC), first_seen="2026-11-01")
            for r in (good, held):
                R.apply(r, R.rules(r["title"], r["description"], D.title_kind(r["title"])))
            with open(os.path.join(d, "jobs.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=scrape.COLS, extrasaction="ignore")
                w.writeheader()
                w.writerow(good)
            self.assertGreaterEqual(int(good["fit_score"]), 50)
            json.dump({"review_ids": ["in-1"], "borderline": [held]}, open(os.path.join(d, "review.json"), "w"))
            json.dump({"ran_at": "x"}, open(os.path.join(d, "feed_status.json"), "w"))
            verdicts = {"in-1": {"score": 35, "category": "Accounting-heavy / unsuitable", "strengths": [], "gap": "mostly close and reconciliations"},
                        "greenhouse:7": {"score": 88, "category": "FP&A / forecasting / budgeting", "strengths": ["annual operating plan"], "gap": ""}}
            calls = []
            R.time.sleep = lambda s: None
            st = R.main(d, os.path.join(d, "none.json"), self.fake_post(verdicts, calls), token="tok", today="2026-11-01")
            out = {r["id"]: r for r in csv.DictReader(open(os.path.join(d, "jobs.csv")))}
            self.assertEqual((st["rescued"], st["demoted"], len(calls)), (1, 1, 1), "both reviewed in one request")
            self.assertEqual(out["greenhouse:7"]["fit_by"], "model")
            self.assertEqual(out["in-1"]["fit_category"], R.ACCT)
            self.assertIn("greenhouse:7", json.load(open(os.path.join(d, "classify_cache.json")))["jobs"])
            self.assertEqual(json.load(open(os.path.join(d, "feed_status.json")))["classifier"]["rescued"], 1)
        finally:
            shutil.rmtree(d)


class Scheduler(unittest.TestCase):
    BASE = [("indeed", "fpa", 60), ("glassdoor", "fpa", 30), ("indeed", "econ", 30)]

    def day(self, n):
        return (date(2026, 11, 1) + timedelta(days=n)).isoformat()

    def run_days(self, st, days, result, pool=()):
        ran = []
        for n in range(days):
            for _ in range(4):
                today = self.day(n)
                plan, dec = S.plan(st, self.BASE, list(pool), today)
                ran.append(plan)
                S.record(st, [dict(site=s, search=t, **result(s, t)) for s, t, _ in plan], today)
        return ran

    def test_empty_site_paused_then_probed_daily_then_resumes(self):
        st = S.load("/nonexistent")
        res = lambda s, t: {"returned": 0 if s == "glassdoor" else 20, "new": 20, "qualified": 5, "qnew": 2 if t == "fpa" else 1}
        ran = self.run_days(st, 3, res)
        self.assertEqual(st["sites"]["glassdoor"]["state"], "paused")
        last_day = [p for p in ran[-4:]]
        self.assertEqual(sum(1 for p in last_day for s, _, _ in p if s == "glassdoor"), 1, "one probe a day")
        S.record(st, [{"site": "glassdoor", "search": "fpa", "returned": 3}], self.day(3))
        self.assertEqual(st["sites"]["glassdoor"]["state"], "active")

    def test_unproductive_search_reduced_and_slot_reused(self):
        st = S.load("/nonexistent")
        res = lambda s, t: {"returned": 20, "new": 10, "qualified": 3, "qnew": 0 if t == "econ" else 4}
        ran = self.run_days(st, 4, res, pool=[("indeed", "budget analyst", 30)])
        self.assertEqual(st["searches"]["indeed|econ"]["state"], "reduced")
        econ_last_day = sum(1 for p in ran[-4:] for s, t, _ in p if t == "econ")
        self.assertEqual(econ_last_day, 1, "once a day")
        self.assertIn("indeed|budget analyst", st["searches"], "a new phrase was put on trial")
        self.assertTrue(any(n > 60 for p in ran[-4:] for s, t, n in p if t == "fpa"), "the productive search asks for more")
        rep = S.report(st, self.day(3))
        self.assertTrue(any(x["search"] == "indeed|fpa" and x["qualified_new_7d"] > 0 for x in rep["searches"]))


class Discovery(unittest.TestCase):
    def test_due_by_yield(self):
        self.assertTrue(D.due({"polls": 20, "hits": 3, "last_hit": "2026-10-30", "ok": "2026-11-01"}, "greenhouse:x", "2026-11-01"))
        self.assertFalse(D.due({"polls": 20, "hits": 0, "nyc": 4, "ok": "2026-11-01"}, "greenhouse:x", "2026-11-01"), "dry board: once a day")
        self.assertTrue(D.due({"polls": 20, "hits": 0, "nyc": 4, "ok": "2026-10-31"}, "greenhouse:x", "2026-11-01"))

    def test_common_crawl_rotates_systems_including_workday(self):
        db = D.load_db("/nonexistent")
        db["cc"] = {"last": "", "candidates": ["greenhouse:old1", "greenhouse:old2"], "cursor": 1}   # v3 layout migrates
        seen = []

        def text(u):
            seen.append(u)
            if u.endswith("collinfo.json"):
                return json.dumps([{"id": "CC-MAIN-2026-40"}])
            if "showNumPages" in u:
                return json.dumps({"pages": 3})
            if "myworkdayjobs" in u:
                return "\n".join(json.dumps({"url": f"https://t{i}.wd5.myworkdayjobs.com/en-US/Ext/job/x_{i}"}) for i in range(3))
            return json.dumps({"url": "https://job-boards.greenhouse.io/newco/jobs/1"})

        def get(url, **k):
            if "greenhouse" in url and "newco" in url:
                return {"jobs": [{"id": 1, "title": "FP&A Analyst", "location": {"name": "Yonkers, NY"}, "absolute_url": "https://job-boards.greenhouse.io/newco/jobs/1"}]} if url.endswith("/jobs") else {"name": "NewCo"}
            if "myworkdayjobs" in url:
                return {"jobPostings": [{"title": "Analyst", "externalPath": "/job/x_1", "locationsText": "Remote, Canada"}]}
            raise ats.HttpError(404, url)

        stats = {"boards_new": 0}
        D.CC_DOMAINS_SAVED = D.CC_DOMAINS
        D.cc_step(db, "2026-11-01", stats, get, text)
        self.assertIn("greenhouse:newco", db["boards"], "a Yonkers job qualifies the board")
        self.assertEqual(db["boards"]["greenhouse:newco"]["fin_at_add"], 1)
        db["cc"]["queue"] = []
        db["cc"]["last"] = ""
        for _ in range(3):
            db["cc"]["last"] = ""
            D.cc_step(db, "2026-11-02", stats, get, text)
        self.assertTrue(any("matchType=domain" in u and "myworkdayjobs" in u for u in seen), "Workday found by a domain query")
        self.assertTrue(any(b.startswith("workday:") for b in db["cc"]["rejected"]), "Canada-only Workday board checked and set aside")

    def test_smartrecruiters_probe(self):
        def get(url, **k):
            if "smartrecruiters.com/v1/companies/ZetaHealth/postings" in url:
                return {"totalFound": 4, "content": []}
            if "smartrecruiters" in url:
                return {"totalFound": 0}
            raise ats.HttpError(404, url)
        self.assertEqual(ats.probe("Zeta Health", get), "smartrecruiters:ZetaHealth")


class EndToEnd(unittest.TestCase):
    def test_columns_hint_and_review_file(self):
        d = tempfile.mkdtemp()
        try:
            recs = [dict(row("in-1", "indeed", "Financial Analyst", "Acme", "Yonkers, NY, USA", desc=FPA_DESC), is_remote=False),
                    dict(row("in-2", "indeed", "Financial Analyst", "Acme", "Stamford, CT, USA", desc=ACCT_DESC), is_remote=False)]

            class DF:
                def to_dict(self, o):
                    return recs
            saved = scrape.SEARCHES[:]
            scrape.SEARCHES[:] = [("indeed", "a", 5)]
            try:
                s = scrape.main(lambda **kw: DF() if kw.get("search_term") == "a" else None, os.path.join(d, "jobs.csv"),
                                os.path.join(d, "feed_status.json"), now=datetime.fromisoformat("2026-11-01T12:00:00+00:00"), direct=False)
            finally:
                scrape.SEARCHES[:] = saved
            rows = {r["id"]: r for r in csv.DictReader(open(os.path.join(d, "jobs.csv")))}
            self.assertEqual(len(rows), 2, "two openings, two places")
            self.assertEqual(rows["in-1"]["location"], "Yonkers, NY, USA (New York metro: Westchester)")
            self.assertEqual(rows["in-1"]["region"], "Westchester")
            self.assertEqual(rows["in-1"]["fit_category"], R.FPA)
            self.assertEqual(rows["in-2"]["fit_category"], R.ACCT)
            self.assertIn("in-2", json.load(open(os.path.join(d, "review.json")))["review_ids"])
            self.assertEqual(s["detail"][0]["qnew"], 1, "one new qualified job (the accounting-heavy one doesn't count)")
            self.assertTrue(os.path.exists(os.path.join(d, "search_stats.json")))
            self.assertEqual(s["regions"].get("Westchester"), 1)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
