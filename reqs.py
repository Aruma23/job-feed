"""What a job asks for, as plain facts (Nov 1 2026): skills and tools, years asked, education, seniority, annual pay.

Facts about the posting only. The matching against Gabriel's experience already lives where his data is: the tracker's
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
_SENIOR = [("intern", r"\bintern"), ("director+", r"\b(director|head of|vice president|vp|chief|cfo)\b"), ("manager", r"\bmanager\b|\blead\b"),
           ("senior", r"\bsenior\b|\bsr\.?\b|\biii\b|\bprincipal\b|\bstaff\b"), ("entry", r"\b(junior|jr\.?|entry|graduate|new grad|associate i)\b|\banalyst i\b")]


def years_asked(text):
    """(floor, ceiling) of the most demanding years requirement, as the gate reads it; (0, 0) when none."""
    t = YRS_SCRUB.sub(" ", str(text or ""))
    floor = ceil = 0
    for m in YRS_RE.finditer(t):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if lo > 20 or hi > 20:
            continue
        if hi >= ceil:
            ceil, floor = hi, lo
    return floor, ceil


def requirements(title, description):
    d = str(description or "").replace("\\n", "\n")
    t = str(title or "")
    skills = [k for k, rx in _RX if rx.search(d)]
    lo, hi = years_asked(d[:12000])
    edu = []
    for label, rx in (("degree", DEGREE_ANY), ("graduate degree", DEGREE_HIGH), ("license/cert", CERT_HARD)):
        m = re.search(r"[^.;\n]*(" + rx.pattern + r")[^.;\n]*", d, re.I)
        if m:
            what = CERT_HARD.search(m.group(0)).group(0).upper() if label == "license/cert" else label
            edu.append(what + (" (preferred)" if re.search(r"prefer|plus|nice to have|a bonus|desired|or equivalent", m.group(0), re.I) else ""))
    level = next((name for name, rx in _SENIOR if re.search(rx, t, re.I)), "")
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
    lo, hi = (lo or hi) * mult, (hi or lo) * mult
    if mult == 1 and hi < 1000:                 # an hourly figure without its interval
        lo, hi = lo * 2080, hi * 2080
    return str(int(round(lo, -2))), str(int(round(hi, -2)))
