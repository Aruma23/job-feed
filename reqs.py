"""What a job asks for, as plain facts (Nov 1 2026): skills and tools, minimum years, education, seniority, annual pay.

These are facts about the posting only. Matching them against Gabriel's own experience (supported / partly supported / no
evidence) happens where his data lives: the tracker reads these columns and compares them with his evidence bank, which is
kept out of this public repository. The skill tags below are the shared vocabulary for both sides.
"""
import re

SKILLS = {
    # methods
    "forecasting": r"forecast", "budgeting": r"budget", "variance analysis": r"variance", "financial modeling": r"financial model|three[- ]statement|3[- ]statement",
    "scenario analysis": r"scenario|sensitivity analys", "long-range planning": r"long[- ]range plan|multi[- ]year plan|strategic plan",
    "annual planning": r"annual (operating )?plan|\baop\b", "headcount planning": r"headcount", "capacity planning": r"capacity plan",
    "workforce management": r"workforce (management|planning)|\bwfm\b|staffing model", "KPI reporting": r"\bkpis?\b|dashboard",
    "executive reporting": r"board (deck|materials|presentation)|executive (presentation|reporting)|leadership (presentation|reporting)",
    "business partnering": r"business partner|partner with", "unit economics": r"unit economics|\bltv\b|\bcac\b|contribution margin",
    "pricing": r"\bpricing\b", "valuation / DCF": r"valuation|\bdcf\b|discounted cash", "M&A": r"\bm&a\b|mergers|acquisitions",
    "cash flow / treasury": r"cash flow forecast|treasury|liquidity", "month-end close": r"month[- ]end|close process",
    "GAAP / accounting": r"\bgaap\b|journal entr|reconcil|general ledger", "data analysis": r"data analys|analy[sz]e (large )?data",
    "automation": r"automat",
    # tools
    "Excel": r"\bexcel\b", "advanced Excel": r"advanced excel|pivot tables?|vlookup|xlookup|index[/ -]match|power query",
    "SQL": r"\bsql\b", "Python": r"\bpython\b", "R": r"\bR\b(?= |,|/)", "VBA / macros": r"\bvba\b|macros",
    "Tableau": r"tableau", "Power BI": r"power ?bi", "Looker": r"looker", "Alteryx": r"alteryx", "Snowflake": r"snowflake",
    "Anaplan": r"anaplan", "Adaptive Planning": r"adaptive (insights|planning)", "Oracle EPM / Hyperion": r"hyperion|essbase|oracle (epm|pbcs)|\bepm\b",
    "Pigment": r"\bpigment\b", "Planful / Vena / OneStream": r"planful|\bvena\b|onestream", "NetSuite": r"netsuite", "SAP": r"\bsap\b",
    "Oracle ERP": r"oracle (erp|financials|cloud)", "Workday": r"workday (financials|hcm|adaptive)", "Salesforce": r"salesforce",
    "Google Sheets": r"google sheets", "PowerPoint": r"powerpoint", "WFM software (NICE, Verint, Genesys, Calabrio)": r"\bnice\b (iex|wfm)|\biex\b|verint|genesys|calabrio|aspect",
    # credentials
    "CPA": r"\bcpa\b", "CFA": r"\bcfa\b", "MBA": r"\bmba\b",
}
_RX = {k: re.compile(v, re.I if k != "R" else 0) for k, v in SKILLS.items()}
_YEARS = re.compile(r"(?:(?:at least|minimum(?: of)?|min\.?)\s*)?(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*(?:years?|yrs?)\b[^.\n]{0,60}?experience"
                    r"|experience[^.\n]{0,25}?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_SENIOR = [("intern", r"\bintern"), ("director+", r"\b(director|head of|vice president|vp|chief|cfo)\b"), ("manager", r"\bmanager\b|\blead\b"),
           ("senior", r"\bsenior\b|\bsr\.?\b|\biii\b|\bprincipal\b|\bstaff\b"), ("entry", r"\b(junior|jr\.?|entry|graduate|new grad|associate i)\b|\banalyst i\b")]


def requirements(title, description):
    d = str(description or "").replace("\\n", "\n")
    t = str(title or "")
    skills = [k for k, rx in _RX.items() if rx.search(d)]
    years = []
    for m in _YEARS.finditer(d[:8000]):
        n = m.group(1) or m.group(3)
        if n and 0 < int(n) <= 15:
            years.append(int(n))
    edu = []
    for label, rx in (("Bachelor's", r"bachelor|\bb\.?[as]\.?\b|undergraduate degree|4[- ]year degree"), ("Master's", r"master'?s|\bm\.?s\.?\b(?! excel| office)"),
                      ("MBA", r"\bmba\b"), ("CPA", r"\bcpa\b"), ("CFA", r"\bcfa\b")):
        m = re.search(r"[^.;\n]*(" + rx + r")[^.;\n]*", d, re.I)
        if m:
            edu.append(label + (" (preferred)" if re.search(r"prefer|plus|nice to have|a bonus|desired", m.group(0), re.I) else ""))
    level = next((name for name, rx in _SENIOR if re.search(rx, t, re.I)), "")
    ymin = min(years) if years else None
    if not level:
        level = "entry" if ymin is not None and ymin <= 1 else "senior" if ymin is not None and ymin >= 5 else "analyst"
    return {"skills": "; ".join(skills), "years_min": "" if ymin is None else str(ymin), "education": "; ".join(edu), "seniority": level}


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
