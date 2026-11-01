"""Every job's history, not just its first-seen date (Nov 1 2026).

history.json keeps, per job id, what it looked like (title, pay, location, remote, and for company-board jobs the description)
and a short event log: new, changed (what changed), closed, reappeared. Columns written to jobs.csv:
  status       new | changed | reappeared (within the last 3 days) | unchanged
  changes      the latest change in words ("pay added: 95000-115000; location: Remote -> New York, NY")
  last_change  date of the latest new / changed / reappeared event
  age_days     days since posted (or first seen when the posting date is unknown)
A job counts as closed only when its company board was read completely without it, or its own page answers 404/410.
A failed or partial read never closes anything, and an aggregator row dropping out of a 72-hour search is not a closure.
"""
import hashlib
import json
import os
import re
from datetime import date

RECENT_DAYS = 3
KEEP_DAYS = 60
MAX_EVENTS = 8


def load(path):
    try:
        h = json.load(open(path, encoding="utf-8"))
        if isinstance(h.get("jobs"), dict):
            return h
    except (FileNotFoundError, ValueError):
        pass
    return {"v": 1, "jobs": {}}


def save(h, path):
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(h, f, separators=(",", ":"), sort_keys=True)
    os.replace(path + ".new", path)


def _h(s):
    s = re.sub(r"[^a-z0-9]+", "", str(s or "").replace("\\n", " ").lower())[:3000]
    return hashlib.sha1(s.encode()).hexdigest()[:10] if s else ""


def _pay(r):
    lo, hi = str(r.get("min_amount") or "").strip(), str(r.get("max_amount") or "").strip()
    return f"{lo}-{hi}".strip("-") + (f" {r.get('interval')}" if (lo or hi) and r.get("interval") else "")


def signature(r, direct):
    loc = re.sub(r" ?\((?:New York metro: [^()]*|Remote, US)\)$", "", str(r.get("location") or ""))
    sig = {"t": str(r.get("title") or "").strip(), "p": _pay(r), "l": loc, "r": str(r.get("is_remote") or "")}
    if direct:                                          # one source, so a changed description is a real edit
        sig["d"] = _h(r.get("description"))
    return sig


def _diff(old, new):
    out = []
    if old.get("t") != new.get("t"):
        out.append(f"title: {old.get('t')} -> {new.get('t')}")
    if old.get("p") != new.get("p"):
        out.append(f"pay added: {new['p']}" if not old.get("p") else ("pay removed" if not new.get("p") else f"pay: {old['p']} -> {new['p']}"))
    if old.get("l") != new.get("l") and old.get("l") and new.get("l"):
        out.append(f"location: {old['l'][:40]} -> {new['l'][:40]}")
    if old.get("r") != new.get("r") and old.get("r") and new.get("r"):
        out.append("now remote" if new["r"] == "True" else "no longer remote")
    if "d" in old and "d" in new and old["d"] and new["d"] and old["d"] != new["d"]:
        out.append("description edited")
    return out


def _event(e, today, kind, detail=""):
    e["ev"] = (e.get("ev", []) + [[today, kind, detail]])[-MAX_EVENTS:]
    if kind in ("new", "changed", "reappeared"):
        e["c"] = today


def update(h, rows, today, closed=(), native=re.compile(r"^[a-z]+:")):
    """rows: everything in this run's feed. closed: ids confirmed closed this run. Fills the history columns on rows."""
    jobs = h["jobs"]
    d0 = date.fromisoformat(today)
    counts = {"new": 0, "changed": 0, "reappeared": 0, "closed": 0}
    for r in rows:
        jid = r.get("id") or ""
        sig = signature(r, bool(native.match(jid)))
        e = jobs.get(jid)
        if e is None:
            e = jobs[jid] = {"f": today, "s": sig}
            _event(e, today, "new")
            counts["new"] += 1
        elif e.get("closed"):
            e.pop("closed")
            _event(e, today, "reappeared", f"closed {e.get('closed_on', '')}".strip())
            e.pop("closed_on", None)
            e["s"] = sig
            counts["reappeared"] += 1
        else:
            ch = _diff(e.get("s") or {}, sig)
            if ch:
                _event(e, today, "changed", "; ".join(ch))
                counts["changed"] += 1
            e["s"] = sig
        e["l"] = today
        last = (e.get("ev") or [[e.get("f", today), "new", ""]])[-1]
        recent = last[1] in ("new", "changed", "reappeared") and (d0 - date.fromisoformat(last[0])).days < RECENT_DAYS
        r["status"] = last[1] if recent else "unchanged"
        r["changes"] = next((ev[2] for ev in reversed(e.get("ev", [])) if ev[1] == "changed"), "")
        r["last_change"] = e.get("c", e.get("f", today))
        posted = str(r.get("date_posted") or "")[:10]
        try:
            r["age_days"] = str((d0 - date.fromisoformat(posted if re.match(r"\d{4}-\d{2}-\d{2}$", posted) else e.get("f", today))).days)
        except ValueError:
            r["age_days"] = ""
    for jid in closed:
        e = jobs.get(jid)
        if e and not e.get("closed"):
            e["closed"] = True
            e["closed_on"] = today
            _event(e, today, "closed")
            counts["closed"] += 1
    for jid in [k for k, e in jobs.items() if (d0 - date.fromisoformat(e.get("l", e.get("f", today)))).days > KEEP_DAYS]:
        del jobs[jid]
    counts["tracked"] = len(jobs)
    return counts
