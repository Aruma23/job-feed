"""Feed watchdog (Nov 14 2026): checks the feed from outside the run it checks.

Why: audit.py checks each run from inside it, so a run that crashes reports nothing, and a part that silently does nothing
(Workday read 0 boards a run from about Nov 10 to 14) looks like a quiet day. On Nov 14 two of four runs failed their output
check and published nothing, and nobody was told. This runs as its own workflow after every "Scrape jobs" run, whatever its
result, and every 6 hours (so a run that never happened is noticed too).

What it does
  1. Reads the last runs from the Actions API and the published feed_status.json.
  2. Checks: the last run's result (failed job, failed step, the error line from its log), failures in a row, how long since
     the feed was last published, collect time against its timeout, each application system (boards due vs read, late,
     degraded), board sweep time, the review step (errors, limits), contract counters (duplicate ids folded, annotation
     errors, accounting or intermediary jobs at A/B), the audit level and the search failure rate.
  3. Heals what is safe to heal: a run that failed for a transient reason (network, timeout, runner lost) in a step that is
     not a check is re-run once. A failed check or failed tests is never re-run: the same code would fail the same way.
  4. Tells: one issue labelled feed-health holds the current findings (GitHub emails the repo owner on new issues and
     comments). A new red finding adds a comment; when everything is clear the issue is closed with a note. Findings carry
     what to do, so the next Claude session (or Gabriel) starts from the cause, not the symptom.
Nothing personal is read or written: the repo is public and so is the issue.

Run: python -I watchdog.py  (env GITHUB_TOKEN, GITHUB_REPOSITORY; reads ./feed_status.json from the checkout)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

WORKFLOW = "scrape.yml"
COLLECT_TIMEOUT_MIN = 50          # scrape.yml collect timeout-minutes
SWEEP_SECONDS = 15 * 60           # discover.SWEEP_SECONDS
STALE_HOURS = 8                   # four runs a day, ~6 h apart: 8 h without a publish means one was missed
LABEL = "feed-health"
BIG_SYSTEMS = {"workday", "oracle", "greenhouse", "lever", "ashby", "smartrecruiters"}   # idle = something broke, not a quiet day
API = "https://api.github.com"

TRANSIENT = re.compile(r"timed? ?out|operation was canceled|connection reset|ECONNRESET|temporary failure|could not resolve host|"
                       r"503 service|502 bad gateway|runner has received a shutdown|lost communication with the server|"
                       r"exceeded the maximum execution time|internal server error|rpc failed|early eof", re.I)
CHECK_STEPS = re.compile(r"check the output|unittest|test", re.I)
ERROR_LINE = re.compile(r"(AssertionError.*|[A-Za-z]+Error: .*|Traceback.*|##\[error\].*|FAILED .*|fatal: .*)")


def _age_hours(iso, now):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (now - t).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def failure_of(jobs, log_text=""):
    """(job name, step name, error line, transient?) for a failed run. jobs: the API's jobs list."""
    for j in jobs:
        if j.get("conclusion") not in ("failure", "timed_out", "cancelled"):
            continue
        step = next((s.get("name", "") for s in j.get("steps") or [] if s.get("conclusion") in ("failure", "timed_out", "cancelled")), "")
        lines = [ln.split("Z ", 1)[-1].strip() for ln in str(log_text).splitlines()]
        errs = [m.group(1) for ln in lines for m in [ERROR_LINE.search(ln)] if m and "Process completed with exit code" not in ln]
        err = (errs[-1] if errs else "")[:300]
        timed = j.get("conclusion") == "timed_out" or "exceeded the maximum execution time" in str(log_text)
        transient = (timed or bool(TRANSIENT.search(err or str(log_text)[-3000:]))) and not CHECK_STEPS.search(step)
        return j.get("name", ""), step, err, transient
    return "", "", "", False


def _f(level, key, title, todo=""):
    return {"level": level, "key": key, "title": title, "todo": todo}


def check(status, runs, now, failure=None, collect_minutes=None):
    """Findings, red first. status: feed_status.json; runs: the API's workflow_runs (newest first)."""
    out = []
    done = [r for r in runs if r.get("status") == "completed"]
    if done and done[0].get("conclusion") != "success":
        job, step, err, transient = failure or ("", "", "", False)
        streak = next((i for i, r in enumerate(done) if r.get("conclusion") == "success"), len(done))
        out.append(_f("red", "run-failed", f"Run {done[0].get('run_number')} {done[0].get('conclusion')}"
                      + (f" in {job} / {step}" if step else "") + (f": {err}" if err else "")
                      + (f" ({streak} failed in a row)" if streak > 1 else ""),
                      "Re-run once (transient)." if transient else "Fix the cause in code: the same run fails the same way."))
    age = _age_hours((status or {}).get("ran_at"), now)
    if age is not None and age > STALE_HOURS:
        out.append(_f("red", "stale", f"The published feed is {age:.0f} hours old (expected every ~6).",
                      "Check whether runs are failing or not starting (GitHub disables schedules on inactive repos)."))
    if collect_minutes and collect_minutes > 0.8 * COLLECT_TIMEOUT_MIN:
        out.append(_f("amber", "collect-slow", f"Collect took {collect_minutes:.0f} of its {COLLECT_TIMEOUT_MIN} minutes.",
                      "Something is growing (boards, searches, harvest); trim it before runs start timing out."))
    if not status:
        return sorted(out, key=lambda f: f["level"] != "red")
    d = status.get("direct") or {}
    for prov, h in sorted((d.get("adapter_health") or {}).items()):
        due, read, late = int(h.get("due") or 0), int(h.get("read") or 0), int(h.get("late") or 0)
        skipped = int(h.get("skipped") or 0)
        if due + late + read + skipped == 0 and h.get("since") and prov in BIG_SYSTEMS:
            out.append(_f("red", f"system-idle-{prov}", f"{prov}: no board read, skipped or even scheduled this run.",
                          "Its boards are never reached: look at the sweep order and budget (Workday, Nov 10-14)."))
        elif due + late + read + skipped == 0 and h.get("since"):
            continue                                     # a tiny system with nothing due this run
        elif due + late >= 20 and read == 0:
            out.append(_f("red", f"system-zero-{prov}", f"{prov}: 0 boards read ({due} due, {late} out of time).",
                          "A whole application system is silent: format change, block, or starved by the sweep budget."))
        elif h.get("state") == "degraded":
            out.append(_f("amber", f"system-degraded-{prov}", f"{prov} degraded: {h.get('why')}", "See direct.adapter_health."))
    due, ok = d.get("boards_due"), d.get("boards_ok")
    if due and ok is not None and due >= 20 and ok < 0.5 * due:
        out.append(_f("amber", "boards-read", f"Read {ok} of {due} boards due.", "See direct.adapter_health and failed_sample."))
    late = sum(int(h.get("late") or 0) for h in (d.get("adapter_health") or {}).values())
    if (d.get("sweep_seconds") and d["sweep_seconds"] > 0.98 * SWEEP_SECONDS) or late >= 20:
        out.append(_f("amber", "sweep-budget", f"The board sweep ran out of time ({d.get('sweep_seconds', 0):.0f} s, {late} boards due left unread);"
                      " the least promising go last.",
                      "Raise SWEEP_SECONDS if collect has room, or read fewer low-yield boards (footprint.py)."))
    if d.get("error"):
        out.append(_f("red", "direct-error", f"The company-board step failed: {d['error']}", "The feed fell back to the last board jobs."))
    c = status.get("classifier") or {}
    if c.get("error") and "no GEMINI_API_KEY" not in c["error"] and not re.search(r"\blimit\b|HTTP 429", c["error"]):
        out.append(_f("amber", "classifier", f"Review step: {c['error'][:200]}", "Rules verdicts stand meanwhile."))
    fn = (c.get("false_negatives") or {}).get("flag")
    if fn:
        out.append(_f("amber", "false-negatives", fn, "Read the sampled fits and loosen the rule that dropped them."))
    dup = status.get("duplicate_ids_merged")
    if dup:
        out.append(_f("amber", "duplicate-ids", f"{dup.get('count')} duplicate ids were folded before writing (e.g. {', '.join(dup.get('ids', [])[:3])}).",
                      "The run was saved, but something upstream still makes a second row under an id: find it."))
    k = status.get("contract") or {}
    if k.get("errors"):
        out.append(_f("amber", "annotate-errors", f"{k['errors']} rows failed annotation (last: {k.get('last_error', '')[:120]}).", ""))
    q = k.get("quality") or {}
    if q.get("a_b_accounting") or q.get("a_b_intermediary"):
        out.append(_f("amber", "quality", f"A/B holds {q.get('a_b_accounting', 0)} accounting-heavy and {q.get('a_b_intermediary', 0)} intermediary jobs.",
                      "priority.py should hold these at C: a path skipped priority.assess."))
    a = status.get("audit") or {}
    if a.get("level") == "block":
        out.append(_f("red", "audit-block", "The run's own audit blocked: " + "; ".join(i.get("title", "") for i in a.get("items", [])
                                                                                      if i.get("level") == "block")[:300],
                      "Jobs were restored from the last feed; find why they vanished."))
    n, failed = int(status.get("searches") or 0), int(status.get("failed") or 0)
    if n and failed > 0.3 * n:
        out.append(_f("amber", "searches", f"{failed} of {n} job-site searches failed.", "A site may be blocking the runner."))
    return sorted(out, key=lambda f: f["level"] != "red")


def render(findings, status, runs, healed=""):
    last = next((r for r in runs if r.get("status") == "completed"), {})
    head = [f"Checked {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC. Last run: #{last.get('run_number')} "
            f"{last.get('conclusion')} ({last.get('created_at')}). Feed published: {(status or {}).get('ran_at')}."]
    if healed:
        head.append(f"Healed: {healed}")
    body = [f"- **{f['level']}** {f['title']}" + (f"  \n  What to do: {f['todo']}" if f["todo"] else "") for f in findings]
    return "\n".join(head + [""] + (body or ["All clear."]) + ["", "<!-- watchdog keys: " + ",".join(f["key"] for f in findings) + " -->"])


# ---------------- GitHub ----------------
def gh(method, path, token, body=None, raw=False):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "job-feed-watchdog"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data.decode("utf-8", "replace") if raw else (json.loads(data) if data else {})


def main():
    token, repo = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("no GITHUB_TOKEN / GITHUB_REPOSITORY: nothing to do")
        return 0
    now = datetime.now(timezone.utc)
    runs = gh("GET", f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=8", token).get("workflow_runs", [])
    try:
        status = json.load(open("feed_status.json", encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    failure, collect_min, healed = None, None, ""
    last = next((r for r in runs if r.get("status") == "completed"), None)
    if last:
        jobs = gh("GET", f"/repos/{repo}/actions/runs/{last['id']}/jobs", token).get("jobs", [])
        col = next((j for j in jobs if j.get("name") == "collect" and j.get("started_at") and j.get("completed_at")), None)
        if col:
            collect_min = (datetime.fromisoformat(col["completed_at"].replace("Z", "+00:00"))
                           - datetime.fromisoformat(col["started_at"].replace("Z", "+00:00"))).total_seconds() / 60
        if last.get("conclusion") != "success":
            bad = next((j for j in jobs if j.get("conclusion") in ("failure", "timed_out", "cancelled")), None)
            log = ""
            if bad:
                try:
                    log = gh("GET", f"/repos/{repo}/actions/jobs/{bad['id']}/logs", token, raw=True)[-20000:]
                except Exception as e:
                    log = f"(log unreadable: {e})"
            failure = failure_of(jobs, log)
            if failure[3] and int(last.get("run_attempt") or 1) == 1:
                try:
                    gh("POST", f"/repos/{repo}/actions/runs/{last['id']}/rerun-failed-jobs", token, {})
                    healed = f"re-ran the failed jobs of run #{last.get('run_number')} once (transient: {failure[2][:120]})"
                except Exception as e:
                    healed = f"tried to re-run run #{last.get('run_number')} and couldn't: {e}"
    findings = check(status, runs, now, failure, collect_min)
    body = render(findings, status, runs, healed)
    open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a", encoding="utf-8").write(body + "\n")
    print(body)
    issues = gh("GET", f"/repos/{repo}/issues?labels={LABEL}&state=open&per_page=5", token)
    issue = issues[0] if issues else None
    reds = {f["key"] for f in findings if f["level"] == "red"}
    title = f"Feed health: {len(findings)} finding(s), {len(reds)} red" if findings else "Feed health: all clear"
    if findings:
        if issue is None:
            try:
                gh("POST", f"/repos/{repo}/labels", token, {"name": LABEL, "color": "d93f0b", "description": "Feed watchdog findings"})
            except urllib.error.HTTPError:
                pass                                     # exists already
            gh("POST", f"/repos/{repo}/issues", token, {"title": title, "body": body, "labels": [LABEL]})
        else:
            before = set(re.findall(r"watchdog keys: ([^ ]*) -->", issue.get("body") or "")[0].split(",")) if "watchdog keys" in (issue.get("body") or "") else set()
            gh("PATCH", f"/repos/{repo}/issues/{issue['number']}", token, {"title": title, "body": body})
            new_red = reds - before
            if new_red or healed:
                gh("POST", f"/repos/{repo}/issues/{issue['number']}/comments", token,
                   {"body": ("New: " + "; ".join(f["title"] for f in findings if f["key"] in new_red) if new_red else "") +
                            (("\n" if new_red else "") + "Healed: " + healed if healed else "")})
    elif issue is not None:
        gh("POST", f"/repos/{repo}/issues/{issue['number']}/comments", token, {"body": "All clear: " + body.splitlines()[0]})
        gh("PATCH", f"/repos/{repo}/issues/{issue['number']}", token, {"state": "closed", "title": title})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                            # its own workflow fails (visible in Actions); the feed is untouched
        print(f"watchdog error: {type(exc).__name__}: {exc}")
        sys.exit(1)
