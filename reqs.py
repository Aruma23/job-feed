"""What a job asks for, as plain facts (Nov 1 2026): skills and tools, years asked, education, seniority, annual pay.

Facts about the posting only. The matching against the job seeker's experience already lives where their data is: the tracker's
gate (engine v28, c_gate.gs) reads his experience library and scores each lead's must-haves as have / transferable / gap.
So the skill names and the years and degree rules here are ported from that gate, word for word, so the feed and the gate
speak the same vocabulary and the gate can use these columns directly.
"""
import re

# [name, pattern] from c_gate.gs SKILLS (the transferable-credit part stays in the gate, with the library)
SKILLS = [
    ("FP&A / corporate finance experience", r"fp ?& ?a\b|\bfpa\b|financial planning|corporate finance|budgeting and forecasting|planning (and|&) analysis"),
    ("Excel", r"\bexcel\b|spreadsheet|pivot tables?|vlookup|xlookup|index[\/ -]?match|sumifs"),
    ("VBA / macros", r"\bvba\b|\bmacros?\b"),
    ("Financial modeling", r"financial model|driver[- ]based|building models|forecast(ing)? models?|modeling skills|build (and maintain )?models"),
    ("Forecasting", r"forecast|projections?|run[- ]rate|re-?forecast"),
    ("Budgeting / planning", r"budget|annual (operating )?plan|\baop\b|long[- ]range plan|planning cycle"),
    ("Variance analysis", r"variance|budget (vs\.?|versus|to) actuals?|\bbva\b|actuals? (vs\.?|versus|to) (budget|forecast|plan)"),
    ("Headcount / workforce planning", r"headcount|workforce planning|capacity planning|staffing (model|plan)|erlang|\bwfm\b|resource planning"),
    ("KPI reporting / dashboards", r"\bkpis?\b|dashboards?|metrics reporting|management reporting|performance reporting"),
    ("Scenario analysis", r"scenario|what[- ]if|sensitivit"),
    ("SQL", r"\bsql\b|snowflake|bigquery|redshift|querying data"),
    ("Tableau", r"tableau"),
    ("Power BI", r"power ?bi"),
    ("Looker / other BI", r"looker|qlik|microstrategy|\bbi tools?\b|business intelligence"),
    ("SAP", r"\bsap\b"),
    ("Oracle / NetSuite / other ERP", r"oracle|netsuite|workday financials|\berp\b"),
    ("Planning software (Anaplan, Adaptive, Hyperion...)", r"anaplan|adaptive (insights|planning)|hyperion|essbase|pigment|planful|onestream|vena|\bcube\b|epm tools?"),
    ("PowerPoint / presenting", r"powerpoint|presentations?|decks?|present (findings|to)"),
    ("Statistics / regression", r"regression|statistic|time[- ]series|econometric"),
    ("Python / R", r"\bpython\b|\br\b(?= |,|/|$)|pandas"),
    ("Automation / process improvement", r"automat|process improvement|streamlin|efficienc"),
    ("Business partnering", r"business partner|partner with|cross[- ]functional|stakeholders?|work closely with"),
    ("Accounting (close, journal entries, GAAP)", r"journal entr|general ledger|\bgl\b|month[- ]end close|accruals?|\bgaap\b|revenue recognition|reconcil"),
    ("Valuation / M&A / DCF", r"valuation|\bdcf\b|m&a|mergers|discounted cash flow|lbo"),
    ("Three-statement modeling", r"three[- ]statement|3[- ]statement|integrated financial statements"),
    ("Pricing analysis", r"pricing (analysis|strategy|model)"),
    ("Unit economics", r"unit economics|contribution margin|\bltv\b|\bcac\b"),
]
_RX = [(k, re.compile(v, re.I)) for k, v in SKILLS]
# years: the gate's rule (the most demanding requirement; degree lengths, company age and time windows are not experience)
YRS_RE = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b[^.;•\n]{0,45}?(?:experience|exp\b|background|"
                    r"building|working|work|in\s|of\s|with\s|managing|leading|supporting|professional|industry|relevant|related|progressive|post)", re.I)
YRS_SCRUB = re.compile(r"\d{1,2}\s*[-–]?\s*year\s+(?:accredited|degree|university|college|program|institution|bachelor)|(?:for|over|more than|nearly|almost)"
                       r"\s+\d{2,3}\s+years|in the (last|past)\s+\d+\s+years|\d+\s*years? old|\d+[- ]year (history|track record|commitment|contract|warranty)", re.I)
DEGREE_ANY = re.compile(r"bachelor|\bba\b|\bbs\b|undergraduate degree|4[- ]year degree|college degree|degree in", re.I)
DEGREE_HIGH = re.compile(r"\bmba\b|master'?s|\bms\b in|graduate degree|\bphd\b", re.I)
CERT_HARD = re.compile(r"\bcpa\b|\bcfa\b|series (7|63|65|66|79|24|86|87)|\bfrm\b|\bcma\b|security clearance|secret clearance|top secret", re.I)
_SENIOR = [("intern", r"\bintern"), ("director+", r"\b(director|head of|vice president|s?vp|evp|chief|cfo)\b|\bc1[5-9]\b"),
           ("manager", r"\bmanager\b|\bteam lead\b|\bsupervisor\b|\bavp\b|\bassistant vice president\b|\bc1[34]\b"),
           ("senior", r"\bsenior\b|\bsr\.?\b|\biii\b|\bprincipal\b|\bstaff\b"), ("entry", r"\b(junior|jr\.?|entry|graduate|new grad|associate i)\b|\banalyst i\b")]
# "Lead" in a title is a manager only with people responsibility in the text (a Finance Lead is usually an individual contributor)
LEAD = re.compile(r"\blead\b", re.I)
MANAGES = re.compile(r"direct reports?|manag(e|ing) (a |the |your )?(team|staff|analysts|people)|team of \d+|people manage|hiring and|"
                     r"performance (reviews?|management)|supervis(e|ing|ion of) (a |the )?(team|staff|analysts)|coach(ing)? and develop|"
                     r"lead(ing)? (a |the )?team of", re.I)
# Preferred-only wording: a sentence or bullet with it (and no required wording) asks softly; a heading with it opens a
# preferred section, a heading with required wording closes it.
# "ideally" hedges; "the ideal candidate will have 5+ years" is how many postings state the requirement, so not "ideal"
PREFERRED = re.compile(r"prefer|nice to have|a plus|plus but|bonus|desired|ideally|would be (great|an advantage)|advantageous", re.I)
REQUIRED = re.compile(r"requir|must|minimum|at least|need|essential|basic qualifications|what you bring|what you'll need|"
                      r"qualifications:|you have", re.I)
NOT_REQUIRED = re.compile(r"\b(?:not|no|non)[- ]?(?:a )?(?:requir\w*|must|minimum|need\w*|essential)\b", re.I)
BULLET = re.compile(r"^[-•*]\s|^\d+[.)]\s")


def _heading(s):
    """'' | 'weak' | 'strong': a short unbulleted line with no sentence punctuation. Strong: ends in a colon, bold or a
    markdown heading (closes any section); weak: a few plain words (opens a preferred section, closes nothing)."""
    if not s or len(s) > 60 or BULLET.match(s) or re.search(r"[.!?]", s.rstrip(".:")) or YRS_RE.search(s):
        return ""
    if s.rstrip().endswith(":") or s.startswith("**") or s.startswith("#"):
        return "strong"
    return "weak" if len(s.split()) <= 4 else ""


def _years(text):
    """Every (floor, ceiling, preferred) years mention in reading order, as years_asked reads them.
    A mention is preferred when its own words say so ("5+ years preferred", "ideally 7 years": the wording before the years
    or in the same comma-segment after them, so "5+ years in FP&A, preferably in insurance" is a required 5) or when it sits
    in a preferred section (a heading with preferred wording, until the next strong heading), unless the mention itself
    says required."""
    out, pref_section = [], False
    for line in YRS_SCRUB.sub(" ", str(text or "")).split("\n"):
        s = line.strip()
        kind = _heading(s)
        if kind:
            if PREFERRED.search(s):
                pref_section = True
            elif kind == "strong" or REQUIRED.search(s):
                pref_section = False
            continue
        for clause in re.split(r"[.;•]", s):
            for m in YRS_RE.finditer(clause):
                lo = int(m.group(1))
                hi = int(m.group(2)) if m.group(2) else lo
                if lo > 20 or hi > 20:
                    continue
                scope = clause[:m.end()] + re.split(r"[,()]", clause[m.end():], 1)[0]
                hard = REQUIRED.search(NOT_REQUIRED.sub(" ", scope))
                out.append((lo, hi, not hard and bool(pref_section or PREFERRED.search(scope))))
    return out


def _most(mentions):
    floor = ceil = 0
    for lo, hi, _ in mentions:
        if hi >= ceil:
            ceil, floor = hi, lo
    return floor, ceil


def years_asked(text):
    """(floor, ceiling) of the most demanding years requirement, as the gate reads it; (0, 0) when none."""
    return _most(_years(text))


def years_required(text):
    """(floor, ceiling) of the most demanding years figure that is not marked preferred; (0, 0) when none is required.
    The same reading as years_asked, minus mentions in a preferred sentence, bullet or section."""
    return _most([m for m in _years(text) if not m[2]])


def years_preferred(text):
    """(floor, ceiling) of the most demanding years figure that is only preferred; (0, 0) when none."""
    return _most([m for m in _years(text) if m[2]])


_CLAUSE_END = re.compile(r"[.;\n]")


def clause_of(rx, d):
    """The clause (between . ; or line breaks) holding rx's first match, or None. Same text as
    re.search(r"[^.;\n]*(" + rx.pattern + r")[^.;\n]*", d), which this replaces (Nov 14): that form re-scans every clause from
    each starting position, about 2 ms a posting per pattern and 97% of requirements()' time (3,000 postings x 3 patterns: ~5
    minutes of every run). Equal as long as rx can't match across . ; or a line break, which none of its callers' patterns can."""
    m = rx.search(d)
    if not m:
        return None
    start = max(d.rfind(".", 0, m.start()), d.rfind(";", 0, m.start()), d.rfind("\n", 0, m.start())) + 1
    end = _CLAUSE_END.search(d, m.end())
    return d[start:end.start() if end else len(d)]


def requirements(title, description):
    d = str(description or "").replace("\\n", "\n")
    t = str(title or "")
    skills = [k for k, rx in _RX if rx.search(d)]
    lo, hi = years_asked(d[:12000])
    edu = []
    for label, rx in (("degree", DEGREE_ANY), ("graduate degree", DEGREE_HIGH), ("license/cert", CERT_HARD)):
        clause = clause_of(rx, d)
        if clause is not None:
            what = CERT_HARD.search(clause).group(0).upper() if label == "license/cert" else label
            edu.append(what + (" (preferred)" if re.search(r"prefer|plus|nice to have|a bonus|desired|or equivalent", clause, re.I) else ""))
    level = next((name for name, rx in _SENIOR if re.search(rx, t, re.I)), "")
    if not level and LEAD.search(t) and MANAGES.search(d):
        level = "manager"
    if not level:
        level = "entry" if hi and lo <= 1 else "senior" if lo >= 5 else "analyst"
    return {"skills": "; ".join(skills), "years_min": (f"{lo}-{hi}" if hi != lo else str(lo)) if hi else "", "education": "; ".join(edu),
            "seniority": level}


def annual_pay(row):
    """(low, high) a year in dollars from the row's pay fields, or ('', '')."""
    def num(x):
        try:
            return float(str(x).replace(",", "").replace("$", ""))
        except ValueError:
            return None
    lo, hi = num(row.get("min_amount") or ""), num(row.get("max_amount") or "")
    iv = str(row.get("interval") or "").lower()
    mult = 2080 if iv.startswith("hour") else 12 if iv.startswith("month") else 52 if iv.startswith("week") else 1
    if lo is None and hi is None:
        return "", ""
    lo, hi = (lo or hi), (hi or lo)
    if not iv and hi < 1000:                    # no interval: cannot tell hourly from malformed/shorthand annual pay
        return "", ""
    lo, hi = lo * mult, hi * mult
    return str(int(lo)), str(int(hi))


# ---------------- v10: pay stated only in the text (NY pay-transparency ranges) ----------------
_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d+)?\s?[kK]|\d{2,3}(?:\.\d{2})?)"
_RANGE = re.compile(_MONEY + r"\s*(?:USD|usd)?\s*(?:-|–|—|to|and)\s*" + _MONEY + r"(?:\s*(?:USD|usd))?(\s*(?:/|per|an|a)\s*(?:hour|hr|year|yr|annum|annually))?", re.I)
_PAY_WORD = re.compile(r"salary|pay|compensation|base|wage|hiring range|annual|range|ote|earn", re.I)
_NOT_PAY = re.compile(r"budget|revenue|portfolio|aum|deal|transaction|raise|funding|bonus pool|million|billion|\bmm\b|assets", re.I)
_NY = re.compile(r"new york|\bnyc\b|\bny\b|manhattan|brooklyn|tri-?state|new jersey|\bnj\b", re.I)
_OTHER_PLACE = re.compile(r"\b(?:california|colorado|washington|illinois|texas|massachusetts|chicago|san francisco|seattle|boston|denver|"
                          r"los angeles|austin|tier ?[23]|zone ?[b-z23])\b", re.I)


def _amount(x):
    x = x.replace(",", "").replace(" ", "")
    return float(x[:-1]) * 1000 if x[-1:] in "kK" else float(x)


def pay_from_text(description):
    """(low, high) a year from a salary range stated in the posting, or ('', ''). Strict, because a wrong range is worse than
    none (the floor check can sink a job): the range must sit next to pay words and not money words (budget, revenue, AUM);
    when the posting gives several ranges by place, only a New York one is used; several unlabeled different ranges -> none.
    Hourly ranges ("/hr", "per hour", or both figures under 200) are annualized at 2,080 hours."""
    text = str(description or "").replace("\\n", "\n")[:12000]
    found = []
    for m in _RANGE.finditer(text):
        ctx = text[max(0, m.start() - 160):m.end() + 40]
        near = text[max(0, m.start() - 60):m.start()]
        if not _PAY_WORD.search(ctx) or _NOT_PAY.search(near):
            continue
        try:
            lo, hi = _amount(m.group(1)), _amount(m.group(2))
        except ValueError:
            continue
        hourly = bool(m.group(3) and re.search(r"hour|hr", m.group(3), re.I))
        if hi < 200 and not hourly:
            continue                                      # $100-$150 could be hourly or shorthand annual salary; do not guess
        if hourly:
            lo, hi = lo * 2080, hi * 2080
        if not (20000 <= lo <= hi <= 1000000) or hi > 4 * lo:
            continue
        place = text[max(0, m.start() - 120):m.start()]
        found.append((lo, hi, bool(_NY.search(place)), bool(_OTHER_PLACE.search(place))))
    if not found:
        return "", ""
    ny = [f for f in found if f[2]]
    if ny:
        lo, hi = ny[0][0], ny[0][1]
    else:
        if all(f[3] for f in found):
            return "", ""                       # only other places' ranges (a California-only disclosure): not this job's
        plain = {(f[0], f[1]) for f in found if not f[3]}
        if len({(f[0], f[1]) for f in found}) > 1 and (len(plain) != 1 or any(f[3] for f in found)):
            return "", ""                       # several places or several ranges, none marked New York: don't guess
        lo, hi = found[0][0], found[0][1]
    return str(int(lo)), str(int(hi))
