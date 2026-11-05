"""The review backlog and the false-negative sample (feed v8.1, Phase 1, Nov 1 2026).

Held jobs. Unconventional company-board titles with some planning signal stay out of the feed until the classify job reviews
them (relevance.py). Review capacity is limited, so:
  - the queue is saved between runs in boards.json under "held" (that file is already published every run). Each entry is the
    job's feed row with the description cut to 4000 characters (all relevance.digest reads, so a saved verdict still matches),
    plus held_since and last_seen;
  - a held job never leaves for lack of review. It leaves only when it has been reviewed, when its board was read in full
    without it (closed, or still listed but no longer borderline), when its own page says it is gone, or when its board left
    the board list. Jobs on boards not read this run (not due, unchanged, failed) are carried;
  - the queue is reviewed most promising first: planning duties, place, how long it has waited and how new the posting is.
    Waiting time keeps adding, so nothing starves.

False-negative sample. Jobs the rules turned away after reading their description (a sales or marketing seat, or too few
planning duties) are pooled each run; a small random sample goes to the model. Fits are rescued into the feed and counted,
so a rule that keeps missing good jobs shows up in the feed status.
"""
import random
from datetime import date

KEEP_CHARS = 4000     # relevance.digest reads the first 4000 characters of the description
PAGE_CHECKS = 10      # held jobs on a partially read board, checked on their own page each run (least recently seen first)
CHECK_AFTER = 3       # days unconfirmed before such a check
POOL_CAP = 3000       # rejected jobs pooled per run (memory bound)
AUDIT_SAMPLE = 16     # rejected jobs sampled per run: 2 requests at 8 jobs a request


def days(a, b):
    try:
        return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days
    except (TypeError, ValueError):
        return 0


def carry(db, held, kept, ok_boards, skipped, present, today, board_of, still_open=None, checks=PAGE_CHECKS):
    """Adds the saved held jobs this run didn't see to `held` (id -> row) unless something shows they are gone. Jobs seen
    again keep their held_since. Returns counts."""
    saved = db.get("held") or {}
    st = {"carried": 0, "closed": 0, "no_longer_borderline": 0, "now_in_feed": 0, "board_removed": 0, "page_checks": 0,
          "carried_not_due": 0}
    for k, r in sorted(saved.items(), key=lambda kv: (kv[1].get("last_seen") or "", kv[0])):
        if k in held:
            held[k]["held_since"] = r.get("held_since") or today
            continue
        if k in kept:                                   # judged a plain fit this run: it is in the feed now
            st["now_in_feed"] += 1
            continue
        b = r.get("_board") or board_of(r.get("job_url"))
        if b not in (db.get("boards") or {}):
            st["board_removed"] += 1
            continue
        if ok_boards.get(b):                            # the whole board was read this run and the job wasn't held again
            if k in present.get(b, ()):
                st["no_longer_borderline"] += 1
            else:
                st["closed"] += 1
            continue
        r = dict(r)
        if b in ok_boards and still_open and st["page_checks"] < checks and days(r.get("last_seen"), today) >= CHECK_AFTER:
            st["page_checks"] += 1                      # partial read (Workday searches): ask the job's own page
            v = still_open(r.get("job_url"))
            if v is False:
                st["closed"] += 1
                continue
            if v:
                r["last_seen"] = today
        held[k] = r
        st["carried"] += 1
        st["carried_not_due"] += 1 if b in skipped or b not in ok_boards else 0
    return st


def store(db, held, reviewed, today, cols):
    """Saves the held jobs still waiting for review (reviewed(row) -> True drops one). Returns the saved count."""
    out = {}
    for k, r in held.items():
        if reviewed(r):
            continue
        row = {c: r.get(c, "") for c in cols if r.get(c, "") not in ("", None)}
        row["description"] = str(r.get("description") or "")[:KEEP_CHARS]
        row["_board"] = r.get("_board", "")
        row["held_since"] = r.get("held_since") or today
        row["last_seen"] = r.get("last_seen") or today
        out[k] = row
    db["held"] = out
    return len(out)


def value(row, today, core):
    """Review order: higher first. core = planning duties the rules counted (relevance.rules(...)["core"])."""
    region = str(row.get("region") or "")
    place = 8 if region.startswith("New York City") else 4 if region == "Remote (US)" else 0 if ("outside" in region.lower() or not region) else 5
    waited = min(30, 2 * max(0, days(row.get("held_since") or today, today)))
    posted = row.get("date_posted") or row.get("first_seen") or ""
    new = 5 if posted and 0 <= days(posted, today) <= 7 else 0
    return 6 * min(int(core or 0), 6) + place + waited + new


def oldest_wait(rows, today):
    return max([days(r.get("held_since") or today, today) for r in rows] or [0])


def sample(pool, k, seed):
    """k items from pool, random but repeatable for the same seed."""
    if len(pool) <= k:
        return list(pool)
    return random.Random(str(seed)).sample(pool, k)
