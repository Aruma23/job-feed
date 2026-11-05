"""Offline tests for v7: the self-audit (restoring unexplained losses, coverage, identity and consistency findings), folding
aggregator copies into board rows, reclassified-not-closed, one board under two spellings, and employer intelligence."""
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
import audit  # noqa: E402
import discover as D  # noqa: E402
import intel  # noqa: E402
import scrape  # noqa: E402

ats.time.sleep = lambda s: None
D.time.sleep = lambda s: None
scrape.time.sleep = lambda s: None
TODAY = "2026-11-01"


def row(**k):
    return dict({c: "" for c in scrape.COLS}, **dict({"last_seen": TODAY, "first_seen": "2026-10-28"}, **k))


def gh(i, **k):
    return row(**dict(dict(id=f"greenhouse:{i}", site="greenhouse", company="Acme", title=f"FP&A Analyst {i}", location="New York, NY",
                           job_url=f"https://boards.greenhouse.io/acme/jobs/{i}"), **k))


class Completeness(unittest.TestCase):
    def test_unexplained_mass_loss_is_restored_and_blocks(self):
        prev = [gh(i) for i in range(100)]
        now = prev[:60]
        out, rep = audit.run(prev, now, set(), TODAY, 4, [], {})
        self.assertEqual((rep["level"], rep["complete"], rep["restored"]), ("block", False, 40))
        self.assertEqual(len(out), 100)
        self.assertIn("40 greenhouse jobs disappeared", rep["items"][0]["title"])
        self.assertIn("INCOMPLETE", audit.markdown(rep, "now"))

    def test_explained_losses_are_not_findings(self):
        prev = [gh(i) for i in range(100)]
        closed = {f"greenhouse:{i}" for i in range(60, 90)}
        merged = row(id="greenhouse:0", company="Acme", title="FP&A Analyst 0", source_ids="greenhouse:0;greenhouse:90;greenhouse:91")
        aged = [dict(p, last_seen="2026-10-20") for p in prev[92:]]
        out, rep = audit.run(prev[:92] + aged, [merged] + prev[1:60], closed, TODAY, 4, [], {})
        self.assertEqual(rep["restored"], 0)
        self.assertFalse([i for i in rep["items"] if i["id"].startswith("lost-")], "closed, merged and aged-out jobs are all explained")

    def test_a_few_losses_are_noted_not_restored(self):
        prev = [gh(i) for i in range(100)]
        out, rep = audit.run(prev, prev[:95], set(), TODAY, 4, [], {})
        self.assertEqual((rep["restored"], rep["complete"]), (0, True))
        self.assertTrue(any(i["id"] == "lost-greenhouse" and i["level"] == "info" for i in rep["items"]))


class Findings(unittest.TestCase):
    def test_coverage_drop_warns(self):
        log = [{"rows_by_site": {"indeed": 300}}] * 5
        items = []
        audit.coverage(log, {"rows_by_site": {"indeed": 90}}, items)
        self.assertEqual(items[0]["level"], "warn")
        self.assertIn("usual 300", items[0]["title"])

    def test_identity_findings(self):
        two = gh(1, source_ids="greenhouse:1;greenhouse:2")
        desc = "Build the annual budget and monthly forecast for the business. " * 10
        a, b = gh(3, description=desc, title="FP&A Analyst"), gh(4, description=desc, title="FP&A Analyst")
        moved = gh(5, company="Other Co")
        items = []
        n = audit.identity_checks([gh(5)], [two, a, b, moved], items)
        self.assertEqual(n, (1, 1, 1))

    def test_stale_priority_found(self):
        r = gh(1, fit_score="90", freshness="fresh", ghost_risk="low", trust="verified", region="New York City", priority="D")
        items = []
        self.assertEqual(audit.consistency([r], items), 1)


class Folding(unittest.TestCase):
    def test_aggregator_copy_folds_into_board_row(self):
        board = gh(101, job_url_direct="https://boards.greenhouse.io/acme/jobs/101", source_ids="greenhouse:101", first_seen="2026-10-30")
        indeed = row(id="in-1", site="indeed", company="Acme Corp.", title="FP&A Analyst 101", location="New York, NY",
                     job_url="https://www.indeed.com/viewjob?jk=1", job_url_direct="https://boards.greenhouse.io/acme/jobs/101?gh_src=x",
                     min_amount="90000", max_amount="110000", interval="yearly", first_seen="2026-10-25")
        other = row(id="in-2", site="indeed", company="Beta", title="Analyst", location="New York, NY", job_url="https://www.indeed.com/viewjob?jk=2")
        folded = scrape.annotate([indeed, other], [board])
        self.assertEqual(folded, {"in-1"})
        self.assertEqual(board["source_ids"], "greenhouse:101;in-1")
        self.assertEqual((board["also_on"], board["first_seen"], board["max_amount"]), ("indeed", "2026-10-25", "110000"))


class Boards(unittest.TestCase):
    def test_one_board_two_spellings(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "boards.json")
            json.dump({"boards": {"ashby:Crusoe": {"hits": 5}, "ashby:crusoe": {"hits": 1}}}, open(p, "w"))
            db = D.load_db(p)
            self.assertEqual(list(db["boards"]), ["ashby:Crusoe"])
            self.assertFalse(D.add_board(db, "ashby:CRUSOE", "Crusoe", "cc", TODAY, {"boards_new": 0}))
        finally:
            shutil.rmtree(d)

    def test_second_spelling_copy_leaves_without_closing(self):
        d = tempfile.mkdtemp()
        try:
            json.dump({"boards": {"ashby:Cohere": {"company": "Cohere", "hits": 3}, "ashby:cohere": {"company": "Cohere"}}},
                      open(os.path.join(d, "boards.json"), "w"))
            prev = [row(id="ashby:a1~cohere", site="ashby", company="Cohere", title="FP&A Analyst", location="New York, NY",
                        job_url="https://jobs.ashbyhq.com/cohere/a1", last_seen="2026-10-31")]

            def api(url, data=None, **k):
                if "posting-api/job-board/Cohere" in url:
                    return {"jobs": [{"id": "a1", "title": "FP&A Analyst", "location": "New York, NY", "jobUrl": "https://jobs.ashbyhq.com/Cohere/a1",
                                      "isListed": True, "descriptionPlain": "Own the forecast, budget and variance analysis."}]}
                raise ats.HttpError(404, url)
            out, held, stats, closed = scrape.direct_step([], prev, TODAY, os.path.join(d, "boards.json"), api, lambda u: u,
                                                          lambda u: (_ for _ in ()).throw(OSError("offline")))
            self.assertEqual([r["id"] for r in out], ["ashby:a1"])
            self.assertEqual(closed, [])
            self.assertIn("ashby:a1~cohere", stats["_explained"])
        finally:
            shutil.rmtree(d)

    def test_still_listed_but_no_longer_matching_is_not_closed(self):
        d = tempfile.mkdtemp()
        try:
            json.dump({"boards": {"greenhouse:acme": {"company": "Acme", "src": "seed"}}}, open(os.path.join(d, "boards.json"), "w"))
            prev = [gh(5, last_seen="2026-10-31")]

            def api(url, data=None, **k):
                if url.endswith("/boards/acme/jobs?content=true"):
                    return {"jobs": [{"id": 5, "title": "Software Engineer", "location": {"name": "New York, NY"},
                                      "absolute_url": "https://boards.greenhouse.io/acme/jobs/5", "company_name": "Acme", "content": "code"}]}
                raise ats.HttpError(404, url)
            out, held, stats, closed = scrape.direct_step([], prev, TODAY, os.path.join(d, "boards.json"), api, lambda u: u,
                                                          lambda u: (_ for _ in ()).throw(OSError("offline")))
            self.assertEqual(closed, [], "still on the board: not a closing")
            self.assertEqual(stats["reclassified"], 1)
            self.assertIn("greenhouse:5", stats["_explained"])
        finally:
            shutil.rmtree(d)


class Intel(unittest.TestCase):
    def test_signals_related_and_exclusions(self):
        ares = [row(id=f"workday:R{i}", company="Ares Management", title=f"Senior Associate, Infrastructure Equity {i}", fit_score="80",
                    date_posted="2026-10-25", priority="A" if i == 1 else "B") for i in range(1, 4)]
        lone = [row(id="greenhouse:9", company="Solo", title="FP&A Analyst", fit_score="80", date_posted="2026-09-01")]
        excluded = [row(id=f"in-s{i}", company="Acme Cable", title="Financial Analyst", fit_score="90", date_posted="2026-10-30") for i in range(4)]
        scam = [row(id=f"in-x{i}", company="Fishy", title="Analyst", fit_score="90", trust="suspicious", date_posted="2026-10-30") for i in range(4)]
        mass = [row(id=f"greenhouse:m{i}", company="Gig Co", title="Finance Consultant", fit_score="70", date_posted="2026-10-01") for i in range(9)]
        rows = ares + lone + excluded + scam + mass
        s = intel.annotate(rows, TODAY, None, D._Excluded(phrases=["acme cable"]))
        names = [e["employer"] for e in s["top"]]
        self.assertEqual(names[0], "Ares Management")
        self.assertNotIn("Acme Cable", names, "an excluded employer never appears")
        self.assertNotIn("Fishy", names)
        self.assertIn("3 posted in 14 days (0 the 14 before)", ares[1]["employer_signal"])
        self.assertEqual(ares[1]["related_ids"], "workday:R1;workday:R3", "best tier first, not itself")
        self.assertEqual(lone[0]["employer_signal"], "", "no threshold met, no signal")
        self.assertIn("copies of one ad", mass[0]["employer_signal"])
        emps, _ = intel.analyze(rows, TODAY, D.NEVER)
        self.assertEqual(next(e["score"] for e in emps if e["employer"] == "Gig Co"), 0, "a mass-posted ad never ranks as a growing team")

    def test_team_of(self):
        self.assertEqual(intel.team_of({"title": "Senior Associate, Financial Planning & Analysis"}), "Financial Planning & Analysis")
        self.assertEqual(intel.team_of({"title": "Financial Analyst - New York"}), "")
        self.assertEqual(intel.team_of({"title": "Analyst", "description": "You will join our Wealth Management team in NYC."}),
                         "Wealth Management")


class EndToEnd(unittest.TestCase):
    def test_reports_written_and_loss_restored(self):
        d = tempfile.mkdtemp()
        keep = scrape.collect.__defaults__
        scrape.collect.__defaults__ = (None, (0, 0), lambda s: None, None)
        try:
            out = os.path.join(d, "jobs.csv")
            prev = [gh(i, last_seen="2026-10-31") for i in range(40)]
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=scrape.COLS)
                w.writeheader()
                w.writerows(prev)

            def none(**k):
                class E:
                    def to_dict(self, o):
                        return []
                return E()
            s = scrape.main(none, out=out, status_path=os.path.join(d, "feed_status.json"), direct=False,
                            now=scrape.datetime(2026, 11, 1, 12, tzinfo=scrape.timezone.utc))
            self.assertEqual((s["audit"]["level"], s["audit"]["restored"], s["partial"]), ("block", 40, True))
            self.assertEqual(len(list(csv.DictReader(open(out, encoding="utf-8")))), 40)
            for f in ("AUDIT.md", "employers.json", "audit_log.json"):
                self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        finally:
            scrape.collect.__defaults__ = keep
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
