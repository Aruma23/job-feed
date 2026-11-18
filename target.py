"""The target profile: who the feed is for, in one place (Nov 18 2026).

Before this file the target lived in five places that had drifted apart: the model's prompt said "analyst, senior analyst or
associate", the scheduler ran "finance and strategy manager" searches, the boards kept manager titles, the ranking took 6
points off for manager level, and a third of what was published was manager level or above. Every test passed, because each
part did what it had been told. Now every part reads the level and the experience reach from here, tests/test_target.py
fails when one of them disagrees, and each run counts what it publishes against this profile (feed_status "target").

To change who the feed is for, change this file. Nothing else needs to know.
"""
import re

import reqs

LEVEL_OUT = ("manager", "director+", "intern")   # reqs.requirements()["seniority"] values that are never published
MAX_REQUIRED_YEARS = 4                           # a posting that REQUIRES more years than this is not published; a figure that
                                                 # is only preferred never counts (reqs.years_required)
PROMPT_LEVEL = "analyst, senior analyst or associate roles"           # the words the review model is given for the level
# Title words the company boards skip before fetching a description (discover.HARD_EXCLUDE adds them).
TITLE_WORDS_OUT = ["manager", "managers", "mgr", "supervisor", "avp", "assistant vice president", "team lead"]
# A search phrase that asks for these is retired (scheduler.plan) and never admitted.
SEARCH_OUT = re.compile(r"\b(manager|director|head of|vice president|vp|avp|supervisor|lead|intern|internship)\b", re.I)


def off(row, fresh=False):
    """'' when the job is inside the target, else why not: 'level: manager' or 'years: requires 6+'.
    fresh: ignore a stored seniority label (a row from an earlier feed was labelled by older patterns)."""
    lvl = (not fresh and row.get("seniority")) or reqs.requirements(row.get("title"), row.get("description"))["seniority"]
    if lvl in LEVEL_OUT:
        return f"level: {lvl}"
    lo, _ = reqs.years_required(str(row.get("description") or "").replace("\\n", "\n")[:12000])
    if lo > MAX_REQUIRED_YEARS:
        return f"years: requires {lo}+"
    return ""


def off_safe(row, fresh=False):
    try:
        return off(row, fresh)
    except Exception:
        return ""


def check(rows):
    """What a finished feed looks like against the profile: violations must be 0 (a row here means a path skipped the gate);
    reach is the share that sits at the edge of the profile (senior level, or the most years still allowed)."""
    bad = [(r, off_safe(r)) for r in rows]
    bad = [(r, why) for r, why in bad if why]
    edge = sum(1 for r in rows if r.get("seniority") == "senior" or
               reqs.years_required(str(r.get("description") or "").replace("\\n", "\n")[:12000])[0] == MAX_REQUIRED_YEARS)
    return {"levels_out": list(LEVEL_OUT), "max_required_years": MAX_REQUIRED_YEARS, "published": len(rows),
            "violations": len(bad), "examples": [f"{r.get('company')}: {r.get('title')} ({why})" for r, why in bad[:5]],
            "reach_share": round(edge / max(1, len(rows)), 3)}
