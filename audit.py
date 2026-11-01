"""Self-audit (feed v7, Nov 1 2026): every run checks its own output before it is published and writes an actionable report.

AUDIT.md (for people) and feed_status.json "audit" (for the tracker and Hub): level ok | warn | block, and items, each saying
what happened, what the feed did about it, and what to do. Checks:

  completeness   jobs in the last published feed that are gone now with no explanation (not closed on a complete read, not
                 aged out, not merged into another row, not held for review, not trimmed by the cap). When a source loses
                 BLOCK_MIN+ jobs that way (and BLOCK_SHARE of its previous rows), they are restored from the last published
                 feed, the run is marked incomplete, and the report says so. Restored rows age out normally after KEEP_DAYS.
  coverage       a source or the board sweep returning far less than its usual (median of the last runs, audit_log.json).
  unconfirmed    jobs still shown that this run couldn't confirm (their board couldn't be read), and the oldest.
  identity       rows that merged two different openings from one board, ids that changed employer, and likely duplicates
                 (same employer, title, place and description under two ids).
  consistency    priority that doesn't match its own inputs (recomputed), and requirement contradictions.
"""
import hashlib
import json
import re
from datetime import date

BLOCK_MIN = 25
BLOCK_SHARE = 0.10
COVERAGE_SHARE = 0.5
LOG_RUNS = 30


def _site(r):
    return (r.get("id") or "").split(":")[0] if re.match(r"^[a-z]+:", r.get("id") or "") else (r.get("site") or "?")


def _item(items, level, key, title, did="", todo="", examples=()):
    items.append({"id": key, "level": level, "title": title, "did": did, "todo": todo, "examples": list(examples)[:5]})


def completeness(prev, rows, explained, today, keep_days):
    """(restored rows, items). explained: ids accounted for (closed, held, trimmed)."""
    import identity
    now_ids = set()
    for r in rows:
        now_ids |= set(identity.source_ids(r))
    d0 = date.fromisoformat(today)
    lost = {}
    for p in prev:
        pid = p.get("id")
        if not pid or pid in now_ids or pid in explained:
            continue
        try:
            if (d0 - date.fromisoformat(p.get("last_seen") or today)).days > keep_days:
                continue                                           # aged out on schedule
        except ValueError:
            pass
        lost.setdefault(_site(p), []).append(p)
    before = {}
    for p in prev:
        before[_site(p)] = before.get(_site(p), 0) + 1
    restored, items = [], []
    for site, ps in sorted(lost.items(), key=lambda kv: -len(kv[1])):
        if len(ps) >= BLOCK_MIN and len(ps) >= BLOCK_SHARE * before.get(site, 1):
            restored += ps
            _item(items, "block", f"lost-{site}", f"{len(ps)} {site} jobs disappeared from this run with no confirmed closing "
                  f"({len(ps)} of {before.get(site)} published last time).",
                  "Restored them from the last published feed and marked this run incomplete, so the tracker doesn't treat "
                  "them as closed.", f"If it repeats, the {site} reader may be broken: check the run log for {site}.",
                  [f"{p.get('company')}: {p.get('title')}" for p in ps])
        elif ps:
            _item(items, "info", f"lost-{site}", f"{len(ps)} {site} job(s) left the feed without a confirmed closing.",
                  "Left out (too few to suggest a broken reader).", "", [f"{p.get('company')}: {p.get('title')}" for p in ps])
    return restored, items


def coverage(log, now, items):
    """Compares this run's counts with the median of earlier runs in audit_log.json."""
    for key, label in (("rows_by_site", "rows"), ("boards", "boards")):
        cur = now.get(key) or {}
        for k, v in cur.items():
            past = sorted(x.get(key, {}).get(k, 0) for x in log[-8:])
            if len(past) < 3:
                continue
            med = past[len(past) // 2]
            if med >= 20 and v < COVERAGE_SHARE * med:
                _item(items, "warn", f"coverage-{key}-{k}", f"{k} brought {v} {label}, against a usual {med}.",
                      "Earlier jobs are kept for a few days, so nothing is lost yet.",
                      f"Check whether {k} is blocking the feed or changed its format.")


def unconfirmed(rows, today, items):
    old = [r for r in rows if (r.get("last_seen") or today) < today]
    if not old:
        return
    oldest = min(old, key=lambda r: r.get("last_seen"))
    _item(items, "info" if len(old) < 0.2 * max(1, len(rows)) else "warn", "unconfirmed",
          f"{len(old)} jobs are shown from earlier runs because this run couldn't confirm them; the oldest was last confirmed "
          f"{oldest.get('last_seen')}.", "Kept for up to 4 days after they were last seen.", "",
          [f"{r.get('company')}: {r.get('title')} (last {r.get('last_seen')})" for r in old[:5]])


def _dhash(r):
    s = re.sub(r"[^a-z0-9]+", "", str(r.get("description") or "").replace("\\n", " ").lower())[:2500]
    return hashlib.sha1(s.encode()).hexdigest()[:12] if len(s) > 200 else ""


def identity_checks(prev, rows, items):
    import identity
    merged_two = []
    for r in rows:
        native = [i for i in identity.source_ids(r) if re.match(r"^[a-z]+:", i)]
        kinds = {}
        for i in native:
            kinds.setdefault(i.split(":")[0], set()).add(i)
        if any(len(v) > 1 for v in kinds.values()):
            merged_two.append(r)
    if merged_two:
        _item(items, "warn", "merged-openings", f"{len(merged_two)} rows hold two different openings from the same board.",
              "Left as they are.", "Identity rules may be merging separate openings; each should be its own row.",
              [f"{r.get('company')}: {r.get('title')} ({r.get('source_ids')})" for r in merged_two])
    prev_co = {p.get("id"): identity.emp_key(p.get("company")) for p in prev}
    moved = [r for r in rows if r.get("id") in prev_co and prev_co[r["id"]] and identity.emp_key(r.get("company")) and
             prev_co[r["id"]] != identity.emp_key(r.get("company")) and not (prev_co[r["id"]] in identity.emp_key(r.get("company"))
                                                                              or identity.emp_key(r.get("company")) in prev_co[r["id"]])]
    if moved:
        _item(items, "warn", "id-employer", f"{len(moved)} job ids now belong to a different employer than last run.",
              "Kept the new value.", "An id must stay with one opening; check the source.",
              [f"{r.get('id')}: now {r.get('company')}" for r in moved])
    groups = {}
    for r in rows:
        h = _dhash(r)
        if h:
            groups.setdefault((identity.fallback_key(r), h), []).append(r)
    dups = [g for g in groups.values() if len(g) > 1]
    if dups:
        _item(items, "info", "likely-duplicates", f"{len(dups)} sets of rows share employer, title, place and description.",
              "Kept apart: employers sometimes post several seats with one description.",
              "If the tracker shows these twice, tell Claude so identity can merge them.",
              [" / ".join(r.get("id") for r in g) + f" ({g[0].get('company')}: {g[0].get('title')})" for g in dups])
    return len(merged_two), len(moved), len(dups)


def consistency(rows, items):
    import priority
    off = []
    for r in rows:
        if not r.get("priority"):
            continue
        total, tier, _ = priority.score(r)
        if tier != r.get("priority"):
            off.append(r)
    if off:
        _item(items, "warn", "priority-stale", f"{len(off)} rows' priority doesn't match their own fit, freshness and requirements.",
              "Left as published.", "Priority should be recomputed after every fit change.",
              [f"{r.get('id')}: {r.get('priority')}" for r in off])
    odd = [r for r in rows if r.get("seniority") == "entry" and re.match(r"([5-9]|1\d)", str(r.get("years_min") or ""))]
    if odd:
        _item(items, "info", "reqs-contradiction", f"{len(odd)} entry-level titles ask for 5+ years.",
              "Ranked by the years asked.", "", [f"{r.get('company')}: {r.get('title')} ({r.get('years_min')} yrs)" for r in odd])
    acct = [r for r in rows if r.get("priority") in ("A", "B") and str(r.get("fit_category", "")).startswith("Accounting")]
    if acct:
        _item(items, "warn", "fit-contradiction", f"{len(acct)} accounting-heavy jobs ranked A or B.", "Left as published.",
              "Fit and category disagree; the relevance rules need a look.", [f"{r.get('company')}: {r.get('title')}" for r in acct])
    return len(off)


def run(prev, rows, explained, today, keep_days, log, now_counts):
    """Returns (rows to publish, report). Never raises."""
    items, restored = [], []
    for name, fn in (("completeness", lambda: completeness(prev, rows, explained, today, keep_days)),):
        try:
            restored, its = fn()
            items += its
        except Exception as e:
            _item(items, "warn", f"audit-{name}", f"The {name} check failed: {type(e).__name__}: {str(e)[:100]}")
    for name, fn in (("coverage", lambda: coverage(log, now_counts, items)), ("unconfirmed", lambda: unconfirmed(rows, today, items)),
                     ("identity", lambda: identity_checks(prev, rows, items)), ("consistency", lambda: consistency(rows, items))):
        try:
            fn()
        except Exception as e:
            _item(items, "warn", f"audit-{name}", f"The {name} check failed: {type(e).__name__}: {str(e)[:100]}")
    have = {r.get("id") for r in rows}
    out = rows + [r for r in restored if r.get("id") not in have]
    level = "block" if any(i["level"] == "block" for i in items) else "warn" if any(i["level"] == "warn" for i in items) else "ok"
    report = {"level": level, "complete": level != "block", "restored": len(out) - len(rows), "checked": len(prev),
              "items": items}
    return out, report


def markdown(report, ran_at):
    head = {"ok": "All checks passed.", "warn": "Published, with warnings.",
            "block": "Published as INCOMPLETE: some jobs were restored from the last good feed."}[report["level"]]
    out = [f"# Feed audit\n\n{ran_at} · **{report['level'].upper()}** · {head}\n"]
    for lv in ("block", "warn", "info"):
        for i in [x for x in report["items"] if x["level"] == lv]:
            out.append(f"## {lv.upper()}: {i['title']}\n")
            if i.get("did"):
                out.append(f"- What the feed did: {i['did']}")
            if i.get("todo"):
                out.append(f"- What to do: {i['todo']}")
            for e in i.get("examples", []):
                out.append(f"  - {e}")
            out.append("")
    if not report["items"]:
        out.append("No findings.\n")
    return "\n".join(out)


def load_log(path):
    try:
        x = json.load(open(path, encoding="utf-8"))
        return x if isinstance(x, list) else []
    except (FileNotFoundError, ValueError):
        return []


def save_log(log, entry, path):
    log = (log + [entry])[-LOG_RUNS:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, separators=(",", ":"))
    return log
