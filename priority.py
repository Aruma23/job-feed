"""Opportunity priority (feed v6, Nov 1 2026): which qualified jobs deserve attention first.

Not another fit score. It starts from fit_score and adds what the feed already knows about whether applying is worth the time:
freshness and ghost risk (high risk caps it at C), employer verification, disclosed pay against the $75k floor and $100k ask, place, years and
credentials asked, seniority, and whether the job is new or reposted. Columns:

  priority       A | B | C | D
  priority_score 0-100
  priority_why   the biggest reasons, in words

It orders; it never decides. The tracker's gate still makes every accept/reject call. A job the gate would reject (a required
CPA/CFA/Series license or MBA, pay below the floor, director level), a suspicious one, or one gone from the
employer's board is D, with the reason; 5+ years required caps it at C (years_min keeps the posting's most demanding figure for the
gate; the ranking reads the text again and sets aside a figure that is only preferred, see reqs.years_required). Whether the job is
already in the tracker and how his experience covers the requirements are private, so the tracker adds those.
"""
import re

FLOOR = 75000
ASK = 100000
TIERS = ((85, "A"), (70, "B"), (55, "C"))
HARD_CERT = re.compile(r"\b(CPA|CFA|SERIES \d+|FRM|CMA|CLEARANCE|SECURITY CLEARANCE|SECRET CLEARANCE|TOP SECRET)\b(?! \(preferred\))", re.I)
MBA_REQ = re.compile(r"\bMBA\b(?! \(preferred\))", re.I)
GRAD_REQ = re.compile(r"(graduate degree|master'?s)(?! \(preferred\))", re.I)


def _int(x):
    try:
        return int(float(str(x).replace(",", "")))
    except ValueError:
        return None


def _years(row):
    """(floor to rank on, the figure in words, preferred floor). years_min holds the posting's most demanding figure (the gate's
    rule); when the text is there, only the figure not marked preferred counts against the job, and a preferred one is a
    softer signal. Without text, years_min decides as before."""
    import reqs
    col = str(row.get("years_min") or "")
    y = re.match(r"(\d+)", col)
    yrs = int(y.group(1)) if y else 0
    text = str(row.get("description") or "").replace("\\n", "\n")[:12000]
    if not text or not yrs:
        return yrs, col, 0
    (rlo, rhi), (plo, _) = reqs.years_required(text), reqs.years_preferred(text)
    if not rhi and not plo:
        return yrs, col, 0                  # the text doesn't say what the column was read from (another source's copy)
    return rlo, (f"{rlo}-{rhi}" if rhi != rlo else f"{rlo}+"), plo


def score(row):
    """(score, tier, reasons). reasons: [(points, words)], largest effect first."""
    fit = _int(row.get("fit_score")) or 0
    pts, gate = [], []

    def add(p, why):
        if p:
            pts.append((p, why))

    f = row.get("freshness")
    add({"fresh": 12, "active": 6, "aging": -4, "stale": -12, "gone": -40}.get(f, 0), {"fresh": "new this week", "active":
        "recent", "aging": "a month or two old", "stale": "listed 60+ days", "gone": "gone from the employer's board"}.get(f, ""))
    add({"high": -20, "medium": -8}.get(row.get("ghost_risk"), 0), f"{row.get('ghost_risk')} ghost-job risk")
    t = row.get("trust")
    add({"verified": 5, "consistent": 2, "unverified": -3, "suspicious": -50}.get(t, 0), {"verified": "employer verified",
        "consistent": "employer consistent", "unverified": "employer unverified", "suspicious": "possible scam"}.get(t, ""))
    hi, lo = _int(row.get("pay_high")), _int(row.get("pay_low"))
    if hi:
        if hi < FLOOR:
            gate.append(f"pay tops out at ${hi:,}, below the ${FLOOR:,} floor")
        elif hi >= ASK:
            add(6, f"pay up to ${hi:,}")
        else:
            add(2, "pay disclosed")
    region = str(row.get("region") or "")
    add(3 if region and not region.startswith("Remote") else -5 if not region else 0, "in the NYC area" if region and
        not region.startswith("Remote") else "place unclear" if not region else "")
    yrs, asked, pref = _years(row)
    if yrs >= 5:
        add(-25 if yrs >= 7 else -15, f"asks for {yrs}+ years (the gate rejects it if required)")
    elif yrs == 4:
        add(-6, "asks for 4 years")
    elif yrs:
        add(2, f"asks for {asked} years" + (f" ({pref}+ preferred)" if pref > yrs else ""))
    elif pref:
        add(-4 if pref >= 5 else 2, f"prefers {pref}+ years, none required")
    edu = str(row.get("education") or "")
    m = HARD_CERT.search(edu)
    if m:
        gate.append(f"requires {m.group(1).upper()}")
    if MBA_REQ.search(edu):
        gate.append("requires an MBA")
    elif GRAD_REQ.search(edu):
        add(-10, "asks for a graduate degree")
    lvl = row.get("seniority")
    if lvl in ("director+", "intern"):
        gate.append({"director+": "director level or above", "intern": "an internship"}[lvl])
    add({"manager": -6, "senior": -3, "entry": 2, "analyst": 2}.get(lvl, 0), f"{lvl} level")
    st = row.get("status")
    add(4 if st in ("new", "changed") else 0, "pay or details changed" if st == "changed" else "new to the feed")
    n = _int(row.get("reposts")) or 0
    add(-6 if n >= 2 else 0, f"reposted {n} times")
    total = max(0, min(100, fit + sum(p for p, _ in pts)))
    tier = next((t for cut, t in TIERS if total >= cut), "D")
    if gate or t == "suspicious" or f == "gone":
        tier = "D"
    if (fit < 50 or row.get("ghost_risk") == "high" or yrs >= 5) and tier in ("A", "B"):
        tier = "C"
    reasons = [(-100, g) for g in gate] + sorted(pts, key=lambda x: -abs(x[0]))
    return total, tier, reasons


def assess(row):
    total, tier, reasons = score(row)
    words = ["likely fails the gate: " + w if p == -100 else ("+ " if p > 0 else "- ") + w for p, w in reasons[:4]]
    row.update(priority=tier, priority_score=str(total), priority_why="; ".join([f"fit {row.get('fit_score') or 0}"] + words))
    return row
