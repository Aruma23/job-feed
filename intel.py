"""Employer opportunity intelligence (feed v7, Nov 1 2026): what the postings say together, not one at a time.

From the jobs in the feed that fit (fit_score >= FIT), per employer: how many openings, how many were posted in the last 14
days against the 14 before, which teams they cluster in, which roles are being hired more than once (several seats or a
repost), and which postings changed pay or title. One title posted MASS+ times (24 "Remote Advisory" seats) is called what it
looks like, a marketplace or staffing listing, and never ranks as a growing team. Only observed evidence: a signal is written only when its threshold is met,
and it says what was counted. Nothing is predicted.

Outputs: employers.json (every employer with a signal, strongest first), feed_status "intel" (the top 10), and two columns:
  employer_signal   e.g. "Ares: 4 fitting openings, 3 posted in 14 days (0 the 14 before); 2 in Infrastructure Equity"
  related_ids       other fitting openings at the same employer (up to 5), so one look covers them all
"""
import json
import re
from datetime import date

FIT = 50
MASS = 8              # this many postings of one title at one employer reads as a marketplace or staffing listing
TEAM = re.compile(r"\b(?:join|within|on|in|part of|supporting)\s+(?:the|our)\s+((?:[A-Z][A-Za-z&/'-]+\s+){0,4}[A-Z][A-Za-z&/'-]+)\s+"
                  r"(?:team|group|division|department|organization|function|business unit|org)\b")
GENERIC_TEAM = {"Finance", "Corporate", "Our", "The", "Global", "Company", "Firm", "Business", "Strategy", "FP&A", "Accounting"}


def _day(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def team_of(row):
    """A team named in the title ('Senior Associate, Infrastructure Equity FP&A' -> after the comma) or in the text ('join our
    Wealth Management team'). '' when none is stated."""
    t = str(row.get("title") or "")
    m = re.search(r"(?:,|\s[-–|]\s)\s*([^,()|–-]{4,60})$", t)
    if m and not re.search(r"\b(new york|nyc|remote|hybrid|ny|nj|ct|onsite|on-site|usa?)\b", m.group(1), re.I):
        part = m.group(1).strip()
        if not re.fullmatch(r"(?i)(senior|sr\.?|junior|jr\.?|i{1,3}|iv|associate|analyst|manager|director|vp|lead)", part):
            return part
    d = str(row.get("description") or "").replace("\\n", " ")[:4000]
    for m in TEAM.finditer(d):
        name = m.group(1).strip()
        if name.split()[0] not in GENERIC_TEAM and len(name) <= 50:
            return name
    return ""


def analyze(rows, today, never=None):
    """Returns (employers sorted strongest first, {row id: (signal, related ids)})."""
    import identity
    d0 = date.fromisoformat(today)
    by = {}
    for r in rows:
        if int(r.get("fit_score") or 0) < FIT or r.get("trust") == "suspicious" or r.get("freshness") == "gone":
            continue
        if never and never.search(str(r.get("company") or "")):
            continue
        k = identity.emp_key(r.get("company"))
        if k:
            by.setdefault(k, []).append(r)
    out, per_row = [], {}
    for k, rs in by.items():
        name = max((r.get("company") or "" for r in rs), key=lambda c: sum(1 for r in rs if r.get("company") == c))
        when = [_day(r.get("date_posted")) or _day(r.get("first_seen")) for r in rs]
        new14 = sum(1 for w in when if w and (d0 - w).days <= 14)
        prev14 = sum(1 for w in when if w and 14 < (d0 - w).days <= 28)
        teams, families, changed = {}, {}, []
        for r in rs:
            t = team_of(r)
            if t:
                teams.setdefault(t, []).append(r["id"])
            families.setdefault(identity.title_ident(r.get("title")), []).append(r)
            if re.search(r"\b(pay|title)\b", str(r.get("changes") or "")) and (d0 - (_day(r.get("last_change")) or d0)).days <= 14:
                changed.append(r)
        signals = []
        mass = max(families.values(), key=len)
        if len(mass) >= MASS:
            signals.append(f"{len(mass)} copies of one ad ({mass[0].get('title')[:60]}): likely a marketplace or staffing listing, "
                           "not a growing team")
        if len(mass) < MASS and (len(rs) >= 3 or (new14 >= 2 and new14 > prev14)):
            signals.append(f"{len(rs)} fitting openings, {new14} posted in 14 days ({prev14} the 14 before)")
        for t, ids in sorted(teams.items(), key=lambda kv: -len(kv[1])):
            if len(ids) >= 2:
                signals.append(f"{len(ids)} in {t}")
        for fam, frs in families.items():
            reposts = sum(int(r.get("reposts") or 0) for r in frs)
            if len(frs) < MASS and (len(frs) >= 2 or reposts):
                signals.append(f"hiring {frs[0].get('title')} " + (f"x{len(frs)}" if len(frs) >= 2 else f"again (reposted {reposts}x)"))
        for r in changed[:2]:
            signals.append(f"{r.get('title')}: {str(r.get('changes'))[:60]}")
        if not signals:
            continue
        senior = {}
        for r in rs:
            senior[r.get("seniority") or "?"] = senior.get(r.get("seniority") or "?", 0) + 1
        pay = max((int(r.get("pay_high") or 0) for r in rs), default=0)
        score = 0 if len(mass) >= MASS else 2 * new14 + len(rs) + 2 * sum(1 for t in teams.values() if len(t) >= 2) + sum(1 for r in rs if r.get("priority") in ("A", "B"))
        e = {"employer": name, "key": k, "openings": len(rs), "new_14d": new14, "prev_14d": prev14, "score": score,
             "teams": {t: len(v) for t, v in teams.items()}, "seniority": senior, "top_pay": pay or "",
             "best": sorted(({"id": r["id"], "title": r.get("title"), "priority": r.get("priority")} for r in rs),
                            key=lambda x: x["priority"] or "Z")[:5], "signals": signals[:6]}
        out.append(e)
        text = f"{name}: " + "; ".join(signals[:3])
        ranked = [x["id"] for x in sorted(rs, key=lambda x: x.get("priority") or "Z")]
        for r in rs:
            per_row[r["id"]] = (text[:240], [i for i in ranked if i != r["id"]][:5])
    out.sort(key=lambda e: -e["score"])
    return out, per_row


def annotate(rows, today, path=None, never=None):
    """Fills employer_signal and related_ids, writes employers.json, returns the feed_status summary. Never raises."""
    try:
        emps, per_row = analyze(rows, today, never)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    for r in rows:
        sig, rel = per_row.get(r.get("id"), ("", []))
        r["employer_signal"] = sig
        r["related_ids"] = ";".join(rel)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"as_of": today, "employers": emps[:200]}, f, indent=0)
    return {"employers_with_signals": len(emps), "top": [{k: e[k] for k in ("employer", "openings", "new_14d", "prev_14d", "signals")}
                                                         for e in emps[:10]]}
