"""Offline tests for v6: freshness and ghost-job risk, employer verification and scam screening, the sudden-drop guard and
system health, priority tiers, and the job record contract (CONTRACT.md)."""
import csv
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
import freshness  # noqa: E402
import history  # noqa: E402
import priority  # noqa: E402
import scrape  # noqa: E402
import trust  # noqa: E402

ats.time.sleep = lambda s: None
D.time.sleep = lambda s: None
scrape.time.sleep = lambda s: None


class FakeDF:
    def __init__(self, recs):
        self.recs = recs

    def to_dict(self, orient):
        return [dict(r) for r in self.recs]

# Real passages from the Nov 1 feed that name WhatsApp, SSNs or wire transfers as products, E-Verify notices or job duties.
REAL_NOT_SCAMS = [
 " Phishing alert: Axon will never ask you to pay for any part of the hiring process, including training, equipment, or background checks. We do not make job offers via text message, WhatsApp, or instant messaging platforms without a formal interview process. All legitimate job openings are listed on our official careers page at https://www.axon.com/careers. If you receive a suspicious offer or outreach from an email address that is not **@axon.com**, or if you are asked for sensitive personal inf",
 "ta:**  Meta builds technologies that help people connect, find communities, and grow businesses. When Facebook launched in 2004, it changed the way people connect. Apps like Messenger, Instagram and WhatsApp further empowered billions around the world. Now, Meta is moving beyond 2D screens toward immersive experiences like augmented and virtual reality to help build the next evolution in social technology. People who choose to build their careers by building with us at Meta help shape a future t",
 "ing you respond to also appears on our official Careers page: **www.sonyjobs.com** . Be advised that we never request personal identifying information (such as Social Security numbers, bank details, or copies of identification documents) during the initial stages of our application process. If you have any doubts about the authenticity of a job posting or communication, please contact **talenthelp@am.sony.com** before submitting any information.  EEO is the Law  EEO is the Law Supplement  Know y",
 "* Prepare and maintain short-term cash flow forecasts to support operational and financial planning. * Perform daily and monthly bank reconciliations, investigating and resolving discrepancies. * Coordinate domestic and international wire transfers, ACH payments, and other banking transactions. * Monitor incoming customer payments and outgoing vendor disbursements to ensure proper cash management. * Assist in managing banking relationships, treasury controls, and financial procedures. * Track fo",
 "ts and account maintenance, by obtaining all necessary approvals and ensuring all client documentation is returned, completed, and approved. * Process all client cashiering requests (money market redemptions, wires, checks, journals, check deposits, etc.). * Support marketing efforts by promoting client seminars and events. * Review daily activity and reconcile current client activity, funds transfers and previous day\u2019s activity and trades against various clearing firm systems to ensure accuracy",
 "**About Meta:**  Meta builds technologies that help people connect, find communities, and grow businesses. When Facebook launched in 2004, it changed the way people connect. Apps like Messenger, Instagram and WhatsApp further empowered billions around the world. Now, Meta is moving beyond 2D screens toward immersive experiences like augmented and virtual reality to help build the next evolution in social technology. People who choose to build their careers by building with us at Meta help shape ",
 "our values HERE https://drive.google.com/file/d/1WbORoU9L17875Re3r0psjPcALDGUvDMa/view. \ud83e\uded0 \ud83d\udc99    Note: Beware of Scammers. Blueberry will NEVER ask you to download software/apps or request sensitive personal information like bank accounts or social security numbers. If you received an email requesting this information it is likely a scam. We are only accepting candidates through our job board.  The Opportunity:  We\u2019re looking for a Senior Manager of Operations to drive critical functions within ou",
 " internal and external auditors to provide necessary documentation and support during audits - Support daily cash management activities, including cash positioning, forecasting, and reporting - Assist in the preparation and processing of wire transfers, ACH payments, and other cash disbursements - Assist in maintaining and monitoring relationships with banking partners - Support the opening, closing, and maintenance of bank accounts and associated services - Help manage bank fee analysis and opt"
]

# An employer's anti-scam notice names the same tricks (FalconX, Nov 1 feed).
REAL_NOT_SCAMS.append("Communicate through official email addresses ending in @falconx.io. If you receive an email from a public "
                      "domain (Gmail, Yahoo, etc.) or a lookalike domain, it is not from us. - Official Channels: We will never conduct "
                      "interviews via Telegram, WhatsApp, or Signal. Our process involves video or in-person meetings. - No Financial "
                      "Requests: FalconX will never ask you for money, \"equipment fees,\" \"training deposits,\" or crypto transfers at any "
                      "stage of the recruitment process.")


def row(**k):
    return dict({c: "" for c in scrape.COLS}, **k)


class Freshness(unittest.TestCase):
    def test_states_and_age_alone_is_never_high(self):
        today = "2026-11-01"
        r = freshness.assess(row(id="greenhouse:1", age_days="3"), {"f": today}, {}, today)
        self.assertEqual((r["freshness"], r["ghost_risk"]), ("fresh", "low"))
        self.assertEqual(freshness.assess(row(id="g:2", age_days="20"), {}, {}, today)["freshness"], "active")
        self.assertEqual(freshness.assess(row(id="g:3", age_days="45"), {}, {}, today)["freshness"], "aging")
        r = freshness.assess(row(id="g:4", age_days="400"), {}, {}, today)
        self.assertEqual((r["freshness"], r["ghost_risk"]), ("stale", "medium"), "age alone: medium at most")
        r = freshness.assess(row(id="g:5", age_days="400", description="This is an evergreen position for our analyst pool."), {}, {}, today)
        self.assertEqual(r["ghost_risk"], "high", "two independent signals")
        e = {"f": "2026-08-01", "c": "2026-10-25", "ev": [["2026-08-01", "new", ""], ["2026-10-25", "changed", "pay added"]]}
        self.assertEqual(freshness.assess(row(id="g:6", age_days="92"), e, {}, today)["freshness"], "active", "a real change keeps it active")

    def test_evergreen_wording_not_finance_terms(self):
        for t in ("Join our talent community to hear about new roles", "We are always accepting applications",
                  "There is no current opening; this posting is to build our pipeline"):
            self.assertTrue(freshness.EVERGREEN.search(t), t)
        for t in ("dedicated FP&A partner for the evergreen funds", "commingled funds, evergreen, and focused strategies",
                  "manage the sales pipeline", "considered for future opportunities that match your skills"):
            self.assertFalse(freshness.EVERGREEN.search(t), t)

    def test_repost_detected_across_runs_but_not_parallel_openings(self):
        h = history.load("/nonexistent")
        base = {"title": "FP&A Analyst", "company": "Acme", "location": "New York, NY", "is_remote": "False"}
        history.update(h, [row(id="greenhouse:1", **base)], "2026-09-01")
        history.update(h, [], "2026-09-20", closed=["greenhouse:1"])
        history.update(h, [row(id="greenhouse:2", **base)], "2026-09-25")
        history.update(h, [], "2026-10-15", closed=["greenhouse:2"])
        r3 = row(id="greenhouse:3", **base)
        history.update(h, [r3], "2026-10-20")
        freshness.assess(r3, h["jobs"]["greenhouse:3"], history.family(h, r3), "2026-10-20")
        self.assertEqual((r3["reposts"], r3["repost_of"]), ("2", "greenhouse:2"))
        self.assertIn("reposted 2 times", r3["freshness_why"])
        # two openings running side by side are separate jobs, not reposts
        a, b = row(id="greenhouse:10", **dict(base, company="Beta")), row(id="greenhouse:11", **dict(base, company="Beta"))
        history.update(h, [a], "2026-10-01")
        history.update(h, [a, b], "2026-10-02")
        freshness.assess(b, h["jobs"]["greenhouse:11"], history.family(h, b), "2026-10-02")
        self.assertEqual((b["reposts"], b["repost_of"]), ("0", ""))

    def test_refresh_counts_redating_but_not_workday_30_plus(self):
        h = history.load("/nonexistent")
        r = row(id="workday:R1", title="FP&A Analyst", company="T", location="New York, NY", date_posted="2026-10-01")
        history.update(h, [dict(r)], "2026-10-31")
        c = history.update(h, [dict(r, date_posted="2026-10-02")], "2026-11-01")   # "30+ days ago" moves a day: not a refresh
        self.assertEqual(c["refreshed"], 0)
        c = history.update(h, [dict(r, date_posted="2026-10-09")], "2026-11-08")   # a weekly read: "30+ days ago" moved 7 days
        self.assertEqual(c["refreshed"], 0)
        c = history.update(h, [dict(r, date_posted="2026-11-12")], "2026-11-13")   # re-dated to yesterday
        self.assertEqual(c["refreshed"], 1)
        rr = [dict(r, date_posted="2026-11-12")]
        history.update(h, rr, "2026-11-13")
        self.assertEqual(rr[0]["status"], "unchanged", "a re-dated ad is not a change")

    def test_route_gone_only_for_complete_reads(self):
        present = {"greenhouse:acme": {"gh:200"}}
        names = {"greenhouse:acme": "Acme"}
        gone = row(id="in-1", job_url_direct="https://boards.greenhouse.io/acme/jobs/100")
        here = row(id="in-2", job_url_direct="https://acme.com/careers?gh_jid=200")
        wd = row(id="in-3", job_url_direct="https://t.wd1.myworkdayjobs.com/Ext/job/NY/FPA_R9")
        self.assertEqual(freshness.route_gone(gone, present, names), "Acme")
        self.assertEqual(freshness.route_gone(here, present, names), "")
        self.assertEqual(freshness.route_gone(wd, dict(present, **{"workday:t|wd1|Ext": set()}), names), "", "Workday never")
        r = freshness.assess(dict(gone, age_days="2"), {}, {}, "2026-11-01", "Acme")
        self.assertEqual((r["freshness"], r["ghost_risk"]), ("gone", "high"))


class Trust(unittest.TestCase):
    def setUp(self):
        self.db = {"boards": {"greenhouse:acme": {"company": "Acme", "src": "cc"}, "greenhouse:xyz123": {"company": "Other Co", "src": "cc"}},
                   "employers": {}}

    def a(self, **k):
        return trust.assess(row(**k), self.db, {}, {}, "2026-11-01", trust.board_index(self.db))

    def test_real_feed_passages_are_not_scams(self):
        for t in REAL_NOT_SCAMS:
            self.assertEqual(trust.scam_signals(t), ([], []), t[:120])

    def test_applicant_directed_scams_are_suspicious(self):
        for t in ("Interviews will be conducted via Telegram with our hiring manager.",
                  "You will receive a check to purchase your home office equipment.",
                  "Purchase the equipment from our vendor and we will send a reimbursement.",
                  "Please provide your bank account details to be considered for the role.",
                  "A training fee of $150 is required before you start.",
                  "Deposit the check with your mobile banking app and send the balance back."):
            r = self.a(id="in-9", company="Acme", description=t, job_url_direct="https://acme.com/jobs/1")
            self.assertEqual(r["trust"], "suspicious", t)

    def test_domains(self):
        self.assertEqual(self.a(id="in-1", company="BlackRock", job_url_direct="https://careers.blackrock.com/job/123")["trust"], "verified")
        self.assertEqual(self.a(id="in-1", company="T-Mobile", job_url_direct="https://careers.t-mobile.com/job/1")["trust"], "verified")
        self.assertEqual(self.a(id="in-1", company="BlackRock", job_url_direct="https://blackrock-careers.net/apply/123")["trust"], "suspicious")
        self.assertEqual(self.a(id="in-1", company="JPMorgan Chase", job_url_direct="https://jpmorgan.xyz/apply")["trust"], "suspicious")
        self.assertEqual(self.a(id="in-1", company="Acme", job_url_direct="https://wa.me/15551234567")["trust"], "suspicious")
        r = self.a(id="in-1", company="Acme", job_url_direct="https://acme.icims.com/jobs/1234/job")
        self.assertEqual(r["trust"], "unverified", "a hiring platform proves nothing by itself")
        self.assertEqual(self.a(id="in-1", company="Acme", job_url_direct="")["trust"], "unverified")

    def test_boards(self):
        self.assertEqual(self.a(id="greenhouse:5", company="Acme", job_url="https://boards.greenhouse.io/acme/jobs/5")["trust"], "consistent")
        r = self.a(id="in-5", company="Acme", job_url_direct="https://boards.greenhouse.io/xyz123/jobs/5")
        self.assertEqual(r["trust"], "unverified")
        self.assertIn("Other Co's board", r["trust_why"])
        self.assertEqual(trust.short_names("Neuberger Berman"), {"neuberger", "nb"})

    def test_overrides(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "domains.txt")
            open(p, "w").write("# note\ngood acmejobs.com\nbad acme-hr.com fake recruiter\n")
            good, bad = trust.load_overrides(p)
            r = trust.assess(row(id="in-1", company="Acme", job_url_direct="https://acmejobs.com/1"), self.db, good, bad, "2026-11-01")
            self.assertEqual(r["trust"], "verified")
            r = trust.assess(row(id="in-1", company="Acme", job_url_direct="https://acme-hr.com/1"), self.db, good, bad, "2026-11-01")
            self.assertEqual(r["trust"], "suspicious")
            self.assertIn("fake recruiter", r["trust_why"])
        finally:
            shutil.rmtree(d)

    def test_official_site_check_is_budgeted_and_verifies(self):
        calls = []

        def get(url, **k):
            calls.append(url)
            if url == "https://acme.com/":
                return '<a href="/careers">Careers</a>'
            if url == "https://acme.com/careers":
                return '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
            raise OSError("offline")
        rows = [row(id=f"greenhouse:{i}", company=c, job_url=f"https://boards.greenhouse.io/{s}/jobs/{i}")
                for i, (c, s) in enumerate([("Acme", "acme"), ("Beta", "beta"), ("Gamma", "gamma")])]
        stats = {}
        trust.verify_employers(self.db, rows, "2026-11-01", stats, get, budget=2)
        self.assertEqual(stats["employers_checked"], 2)
        self.assertEqual(self.db["employers"]["acme"]["linked"], ["greenhouse:acme"])
        self.assertIn("unreachable", self.db["employers"]["beta"]["result"])
        self.assertEqual(self.a(id="greenhouse:0", company="Acme", job_url="https://boards.greenhouse.io/acme/jobs/0")["trust"], "verified")
        n = len(calls)
        trust.verify_employers(self.db, rows[:1], "2026-11-10", {}, get)
        self.assertEqual(len(calls), n, "rechecked monthly, not every run")


class Guard(unittest.TestCase):
    def jobs(self, n, complete=True):
        j = ats.Jobs({"id": f"greenhouse:{i}", "title": "x", "location": "NY", "url": "u"} for i in range(n))
        j.complete = complete
        return j

    def test_sudden_drop_keeps_snapshot_until_it_repeats(self):
        info = {"hist": [80, 82, 80]}
        out, complete, note = D.guard(info, self.jobs(12))
        self.assertFalse(complete)
        self.assertIn("sudden drop", note)
        D.guard(info, self.jobs(12))
        out, complete, note = D.guard(info, self.jobs(12))
        self.assertTrue(complete, "a shrink seen three reads in a row is real")
        self.assertEqual(info["hist"], [12])
        out, complete, note = D.guard({"hist": [5, 5, 5]}, self.jobs(1))
        self.assertTrue(complete, "small boards can shrink")

    def test_repeated_pages_deduped(self):
        j = self.jobs(3)
        j.extend(self.jobs(3))
        out, complete, note = D.guard({}, j)
        self.assertEqual((len(out), note), (3, "repeated pages"))

    def test_drop_closes_nothing_and_flags_the_system(self):
        d = tempfile.mkdtemp()
        try:
            boards = {f"greenhouse:b{i}": {"company": f"B{i}", "src": "cc", "hist": [40, 40, 40]} for i in range(3)}
            json.dump({"boards": boards}, open(os.path.join(d, "boards.json"), "w"))
            prev = [row(id="greenhouse:900", site="greenhouse", title="FP&A Analyst", company="B0", location="New York, NY",
                        job_url="https://boards.greenhouse.io/b0/jobs/900", last_seen="2026-10-31", first_seen="2026-10-20")]

            def api(url, data=None, **k):
                if "/boards/b" in url and url.endswith("content=true"):
                    return {"jobs": [{"id": 1, "title": "Engineer", "location": {"name": "Boston"}, "absolute_url": "https://x/1",
                                      "company_name": "B"}]}
                raise ats.HttpError(404, url)
            out, held, stats, closed = scrape.direct_step([], prev, "2026-11-01", os.path.join(d, "boards.json"), api, lambda u: u,
                                                          lambda u: (_ for _ in ()).throw(OSError("offline")))
            self.assertEqual(closed, [], "a suspect read closes nothing")
            self.assertEqual([r["id"] for r in out], ["greenhouse:900"], "last good snapshot kept")
            self.assertEqual(stats["drops"], 3)
            self.assertEqual(stats["adapters_degraded"], ["greenhouse"])
            self.assertEqual(json.load(open(os.path.join(d, "boards.json")))["adapters"]["greenhouse"]["state"], "degraded")
        finally:
            shutil.rmtree(d)


class Priority(unittest.TestCase):
    def test_tiers(self):
        good = row(fit_score="78", freshness="fresh", ghost_risk="low", trust="verified", pay_high="120000", region="New York City",
                   years_min="2-4", seniority="analyst", status="new")
        self.assertEqual(priority.assess(dict(good))["priority"], "A")
        self.assertEqual(priority.assess(dict(good, years_min="5-7"))["priority"], "C", "5+ years: capped, the gate decides")
        for k, v, why in (("education", "degree; CPA", "CPA"), ("pay_high", "60000", "floor"),
                          ("trust", "suspicious", "scam"), ("freshness", "gone", "gone"), ("seniority", "director+", "director")):
            r = priority.assess(dict(good, **{k: v}))
            self.assertEqual(r["priority"], "D", why)
        self.assertNotEqual(priority.assess(dict(good, education="degree; CPA (preferred)"))["priority"], "D", "preferred never gates")
        self.assertEqual(priority.assess(dict(good, fit_score="40"))["priority"], "C", "a weak fit never tops the list")
        r = priority.assess(dict(good, fit_score="98", freshness="stale", ghost_risk="high"))
        self.assertEqual(r["priority"], "C", "high ghost risk never tops the list")
        self.assertIn("ghost-job risk", r["priority_why"])


class Contract(unittest.TestCase):
    def test_columns_and_values(self):
        doc = open(os.path.join(os.path.dirname(HERE), "CONTRACT.md"), encoding="utf-8").read()
        for c in scrape.COLS:
            self.assertIn(f"`{c}`", doc, f"{c} is not in CONTRACT.md")
        self.assertIn(f"Contract version: {scrape.CONTRACT_VERSION}", doc)

    def test_end_to_end_values(self):
        d = tempfile.mkdtemp()
        try:
            recs = [{"site": "indeed", "title": "FP&A Analyst", "company": "BlackRock", "location": "New York, NY", "is_remote": False,
                     "date_posted": "2026-10-31", "job_url": "https://www.indeed.com/viewjob?jk=abc12345",
                     "job_url_direct": "https://careers.blackrock.com/job/123",
                     "description": "Build the forecast and annual budget; variance analysis; financial model. 2+ years.",
                     "id": "in-abc12345"}]

            def scrape_fn(**k):
                return FakeDF(recs if k.get("search_term") == "FP&A analyst" and k["site_name"] == ["indeed"] else [])
            keep = scrape.collect.__defaults__
            scrape.collect.__defaults__ = (None, (0, 0), lambda s: None, None)
            self.addCleanup(setattr, scrape.collect, "__defaults__", keep)
            scrape.main(scrape_fn, out=os.path.join(d, "jobs.csv"), status_path=os.path.join(d, "feed_status.json"),
                        direct=False, now=scrape.datetime(2026, 11, 1, 12, tzinfo=scrape.timezone.utc))
            rows = list(csv.DictReader(open(os.path.join(d, "jobs.csv"), encoding="utf-8")))
            check_values(self, rows)
            self.assertEqual(rows[0]["trust"], "verified")
            st = json.load(open(os.path.join(d, "feed_status.json")))
            self.assertEqual(st["contract"]["version"], scrape.CONTRACT_VERSION)
            self.assertIn("uncovered_hosts", st["coverage"])
        finally:
            shutil.rmtree(d)


class AfterReview(unittest.TestCase):
    def test_rescued_rows_filled_and_priority_follows_final_fit(self):
        d = tempfile.mkdtemp()
        try:
            json.dump({"boards": {"greenhouse:acme": {"company": "Acme"}}}, open(os.path.join(d, "boards.json"), "w"))
            demoted = row(id="in-1", fit_score="30", freshness="fresh", ghost_risk="low", trust="verified", priority="A", status="new", reposts="0",
                          job_url="https://x/1")
            rescued = row(id="greenhouse:7", company="Acme", title="Strategy & Operations Associate", fit_score="72", status="new",
                          date_posted="2026-10-30", job_url="https://boards.greenhouse.io/acme/jobs/7", region="New York City")
            scrape.after_review([demoted, rescued], [rescued], d, "2026-11-01")
            self.assertEqual(demoted["priority"], "D", "priority follows the reviewed fit")
            check_values(self, [demoted, rescued])
            self.assertEqual((rescued["freshness"], rescued["trust"]), ("fresh", "consistent"))
        finally:
            shutil.rmtree(d)


ENUMS = {"freshness": {"fresh", "active", "aging", "stale", "gone"}, "ghost_risk": {"low", "medium", "high"},
         "trust": {"verified", "consistent", "unverified", "suspicious"}, "priority": {"A", "B", "C", "D"},
         "status": {"new", "changed", "reappeared", "unchanged"}}


def check_values(t, rows):
    ids = [r["id"] for r in rows]
    t.assertEqual(len(ids), len(set(ids)), "one row per canonical id")
    for r in rows:
        for col, ok in ENUMS.items():
            t.assertIn(r[col], ok, f"{r['id']} {col}={r[col]!r}")
        t.assertTrue(r["job_url"].startswith("http"))
        t.assertTrue(r["reposts"].isdigit())
        t.assertTrue(0 <= int(r["priority_score"]) <= 100)


if __name__ == "__main__":
    unittest.main()
