"""Freshness and ghost-job risk (feed v6, Nov 1 2026).

A job still listed is not necessarily a job still being filled. This module reads what the feed already knows (posting date,
history.json's events and families, and whether the employer's own board still lists the job) and writes:

  freshness       fresh (<= 7 days) | active (<= 30 days, or a real change in the last 14) | aging (<= 60) | stale (> 60)
                  | gone (the employer's own board, read completely, no longer lists it)
  ghost_risk      low | medium | high. Age alone never makes it high: high needs two independent signals, or "gone".
  freshness_why   the signals in words
  reposts         how many earlier ids of the same employer + title + place ended before this one began (last 180 days)
  repost_of       the most recent of those ids, so the tracker can carry its decision instead of showing a "new" job

Signals: posted 120+ days ago; evergreen/pipeline wording; reposted 2+ times; re-dated 3+ times with no real change in 30 days.
No requests are made here.
"""
import re
from datetime import date

FRESH, ACTIVE, AGING = 7, 30, 60
OLD = 120
EVERGREEN = re.compile(
    r"\bevergreen (?:role|position|posting|requisition|req|opening|job)\b|is an evergreen\b(?! fund)|talent (?:community|pool|pipeline) (?:posting|requisition|role|position)|"
    r"(?:join|submit[^.\n]{0,30}to|apply[^.\n]{0,30}to) our talent (?:community|pool|network)|"
    r"always (?:accepting|taking) (?:applications|resumes)|accepting applications on a rolling basis for future|"
    r"(?:no|not an?) (?:current|immediate|active) (?:opening|openings|vacanc(?:y|ies)|position)|"
    r"(?:this|the) (?:posting|requisition|role|position) is (?:for|to build) (?:a |our )?(?:pipeline|bench|future)|"
    r"for (?:future|upcoming) (?:openings|opportunities|roles|positions) only|general (?:application|interest) (?:posting|form|requisition)",
    re.I)


def _days(d0, s):
    try:
        return (d0 - date.fromisoformat(str(s)[:10])).days
    except ValueError:
        return None


def reposts_of(jid, fam, first, d0, native=re.compile(r"^[a-z]+:")):
    """Earlier ids of the same family that ended before this one began: closed (confirmed), or, for aggregator ids that are
    never 'closed', not seen for 7+ days before this one first appeared. Ids that ran side by side are separate openings."""
    out = []
    for oid, m in (fam or {}).items():
        if oid == jid or m[0] >= first:
            continue
        closed = m[2] if len(m) > 2 else ""
        if closed:
            ended = closed if closed <= first else ""
        else:
            gap = _days(date.fromisoformat(first), m[1])
            ended = m[1] if (gap is not None and gap >= (1 if native.match(oid) else 7)) else ""
        if ended and (_days(d0, ended) or 0) <= 180:
            out.append((ended, oid))
    return sorted(out)


def assess(row, entry, fam, today, route_gone="", ):
    """Fills the freshness columns on row. entry: the row's history.json entry; fam: history.family(); route_gone: the employer
    board's name when its complete read no longer lists this job."""
    d0 = date.fromisoformat(today)
    entry = entry or {}
    first = entry.get("f", today)
    age = int(row["age_days"]) if str(row.get("age_days") or "").lstrip("-").isdigit() else _days(d0, first)
    since_change = _days(d0, entry.get("c", first))
    earlier = reposts_of(row.get("id"), fam, first, d0)
    refreshed = entry.get("rf", 0)
    signals = []
    if age is not None and age > OLD:
        signals.append(f"posted {age} days ago")
    if EVERGREEN.search(str(row.get("description") or "").replace("\\n", " ")):
        signals.append("reads like an evergreen or pipeline posting")
    if len(earlier) >= 2:
        signals.append(f"reposted {len(earlier)} times in 6 months")
    if refreshed >= 3 and (since_change or 0) > 30:
        signals.append(f"re-dated {refreshed} times with no real change")
    if route_gone:
        state, risk = "gone", "high"
        signals.insert(0, f"no longer listed on {route_gone}'s own board")
    else:
        if age is None:
            state = "active"
        elif age <= FRESH:
            state = "fresh"
        elif age <= ACTIVE or (since_change is not None and since_change <= 14 and entry.get("ev") and
                               any(ev[1] == "changed" for ev in entry["ev"])):
            state = "active"
        elif age <= AGING:
            state = "aging"
        else:
            state = "stale"
        risk = "high" if len(signals) >= 2 else "medium" if signals else "low"
    basis = f"{age} days old" if age is not None else "posting date unknown"
    if len(earlier) == 1:
        signals.append("reposted once")
    row.update(freshness=state, ghost_risk=risk, freshness_why="; ".join([basis] + signals),
               reposts=str(len(earlier)) if earlier else "0", repost_of=earlier[-1][1] if earlier else "")
    return row


def board_keys(jobs):
    """Every requisition key a complete board read exposes (its jobs' links and native ids)."""
    import identity
    keys = set()
    for j in jobs:
        keys |= identity.ats_keys(j.get("url"))
        kind, _, num = str(j.get("id") or "").partition(":")
        if kind in identity._NATIVE and num:
            keys.add(identity._NATIVE[kind] + ":" + num.lower())
    return keys


ROUTE_FAMILIES = ("gh:", "lever:", "ashby:", "sr:", "workable:", "rippling:")


def route_gone(row, present, names):
    """The employer board's name when this aggregator row's employer link points at a board read completely this run that no
    longer lists the job. Workday reads are partial, so they never count."""
    import ats
    import identity
    u = row.get("job_url_direct") or ""
    b = ats.board_of(u)
    if not b or b not in present:
        return ""
    keys = {k for k in identity.ats_keys(u) if k.startswith(ROUTE_FAMILIES)}
    if not keys or keys & present[b]:
        return ""
    return names.get(b) or b.split(":", 1)[1]
