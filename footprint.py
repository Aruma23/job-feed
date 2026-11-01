"""Collection footprint and source access profiles (feed v8, Nov 1 2026).

Every employer board is read through its documented public API (no browser, no career-page scraping). This module decides
whether a read should happen at all, and records how each source publishes.

Access profile, kept on each board in boards.json:
  sig / same      a fingerprint of the board's job list and how many reads in a row it hasn't changed
  every / next    read every N runs (1 when it changed or brought a new fitting job; doubles while unchanged, up to 2 runs
                  for boards with a match in 14 days and 8 runs otherwise) and the run it is next due
  yield           new fitting jobs per read, last 10 reads
  fields          share of the last read's jobs carrying pay, posting date, location (and descriptions on fitting jobs)
  errs            the last failure kinds; a failing board backs off 2, 4, 8 runs before the weekly rule takes over
Never longer than MAX_DAYS between reads of a board, so its jobs stay confirmed inside the feed's 4-day window.

sources.json: the source graph, employer -> own website (and whether it links to the board) -> board -> public endpoint ->
fitting jobs, with each board's profile and role (primary, secondary when the same employer has a more productive board,
failing, quiet), plus employers seen only through job sites with the apply site the feed can't read yet.
feed_status "direct.footprint": boards read and skipped as unchanged, new fitting jobs per 100 requests by system.
"""
import hashlib
import json
from datetime import date

MAX_EVERY_HOT = 2
MAX_EVERY = 8
MAX_DAYS = 2
ENDPOINT = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{0}/jobs", "lever": "https://api.lever.co/v0/postings/{0}",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{0}", "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{0}/postings",
    "workday": "https://{0}.{1}.myworkdayjobs.com/wday/cxs/{0}/{2}/jobs", "workable": "https://apply.workable.com/api/v1/widget/accounts/{0}",
    "breezy": "https://{0}.breezy.hr/json", "recruitee": "https://{0}.recruitee.com/api/offers/", "teamtailor": "https://{0}.teamtailor.com/jobs.json",
    "pinpoint": "https://{0}.pinpointhq.com/postings.json", "rippling": "https://ats.rippling.com/api/v2/board/{0}/jobs",
    "personio": "https://{0}.jobs.personio.de/search.json"}


def endpoint(board):
    kind, _, rest = board.partition(":")
    return ENDPOINT.get(kind, "").format(*rest.split("|")) if kind in ENDPOINT else ""


def hot(info, today):
    return bool(info.get("last_hit")) and (date.fromisoformat(today) - date.fromisoformat(info["last_hit"])).days <= 14


def due(info, run, today):
    """Footprint gate, applied after the schedule in discover.due(): skip a board until its next run, but never let a
    readable board go MAX_DAYS without a read."""
    if not info.get("next") or run >= info["next"]:
        return True
    if info.get("ok"):
        try:
            return (date.fromisoformat(today) - date.fromisoformat(info["ok"])).days >= MAX_DAYS
        except ValueError:
            return True
    return True


def signature(jobs):
    s = "|".join(sorted(f"{j.get('id')}~{j.get('title')}~{j.get('location')}" for j in jobs))
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def after_read(info, jobs, hits, run, today, suspect=False):
    """Updates the profile after a successful read. Returns the number of new fitting jobs."""
    sig = signature(jobs)
    changed = sig != info.get("sig")
    info["sig"] = sig
    info["same"] = 0 if changed else info.get("same", 0) + 1
    seen = set(info.get("hit_ids", []))
    new = [j["id"] for j in hits if j["id"] not in seen]
    info["hit_ids"] = (info.get("hit_ids", []) + new)[-60:]
    info["yield"] = (info.get("yield", []) + [len(new)])[-10:]
    cap = MAX_EVERY_HOT if (hot(info, today) or new) else MAX_EVERY
    info["every"] = 1 if (changed or new or suspect) else min(cap, 2 ** info["same"])
    info["next"] = run + info["every"]
    n = max(1, len(jobs))
    info["fields"] = {"pay": round(sum(1 for j in jobs if j.get("pay_min") or j.get("pay_max")) / n, 2),
                      "posted": round(sum(1 for j in jobs if j.get("posted")) / n, 2),
                      "loc": round(sum(1 for j in jobs if j.get("location")) / n, 2)}
    if hits:
        info["fields"]["desc"] = round(sum(1 for j in hits if (j.get("description") or "").strip()) / len(hits), 2)
    return len(new)


def after_failure(info, err, run):
    info["errs"] = (info.get("errs", []) + [str(err)[:60]])[-5:]
    info["every"] = min(MAX_EVERY, 2 ** min(info.get("fails", 1), 3))
    info["next"] = run + info["every"]


def report(per, skipped, run):
    """feed_status direct.footprint."""
    import ats
    out = {"run": run, "skipped_unchanged": skipped, "by_system": {}}
    for prov, h in sorted(per.items()):
        req = (ats.STATS.get(prov) or {}).get("requests") or sum(v.get("requests", 0) for k, v in ats.STATS.items() if k.startswith(prov))
        out["by_system"][prov] = {"read": h.get("read", 0), "skipped": h.get("skipped", 0), "requests": req,
                                  "new_fitting": h.get("new", 0), "per_100_requests": round(100 * h.get("new", 0) / req, 1) if req else None}
    out["read"] = sum(v["read"] for v in out["by_system"].values())
    out["new_fitting"] = sum(v["new_fitting"] for v in out["by_system"].values())
    return out


def source_graph(db, rows, coverage_hosts, today, path):
    """Writes sources.json (employer -> site -> boards -> endpoint -> fitting jobs). Returns a summary."""
    import trust
    emps = db.get("employers", {})
    fitting = {}
    for r in rows:
        if int(r.get("fit_score") or 0) >= 50:
            fitting.setdefault(trust.emp_tokens(r.get("company"))[0], []).append(r.get("id"))
    graph = {}
    for b, info in db.get("boards", {}).items():
        if not (info.get("nyc") or info.get("hits")):
            continue
        k = trust.emp_tokens(info.get("company"))[0] or b
        e = graph.setdefault(k, {"employer": info.get("company") or b, "boards": []})
        y = sum(info.get("yield", []))
        e["boards"].append({"board": b, "system": b.split(":")[0], "endpoint": endpoint(b), "fields": info.get("fields", {}),
                            "last_ok": info.get("ok", ""), "fails": info.get("fails", 0), "errs": info.get("errs", [])[-2:],
                            "every_runs": info.get("every", 1), "unchanged_reads": info.get("same", 0), "new_fitting_10_reads": y,
                            "jobs": info.get("jobs", 0), "local_jobs": info.get("nyc", 0), "matches": info.get("hits", 0)})
    roles = {"primary": 0, "secondary": 0, "failing": 0, "quiet": 0}
    for k, e in graph.items():
        rec = emps.get(k) or {}
        e["site"] = rec.get("domain", "")
        e["site_links_board"] = rec.get("linked", [])
        e["fitting_jobs"] = fitting.get(k, [])[:20]
        e["boards"].sort(key=lambda x: (-x["new_fitting_10_reads"], -x["matches"], -x["local_jobs"]))
        for i, x in enumerate(e["boards"]):
            x["role"] = ("failing" if x["fails"] >= 3 else "primary" if i == 0 and (x["matches"] or x["local_jobs"])
                         else "secondary" if i > 0 else "quiet")
            roles[x["role"]] += 1
    for h in coverage_hosts:
        if h.get("qualified") and h.get("host") not in ("(no employer link)",):
            graph.setdefault("site:" + h["host"], {"employer": h["host"], "boards": [], "site": h["host"],
                                                   "note": f"seen only through job sites; {h['qualified']} fitting jobs apply on {h['host']}, "
                                                           "which has no public endpoint the feed reads yet"})
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"as_of": today, "employers": graph}, f, separators=(",", ":"), sort_keys=True)
    return {"employers": len(graph), "boards": sum(len(e["boards"]) for e in graph.values()), "roles": roles,
            "employers_with_2_boards": sum(1 for e in graph.values() if len(e["boards"]) > 1)}
