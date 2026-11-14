"""Who is behind a listing (feed v10, Nov 14 2026): the company hiring for its own team, or an intermediary.

A legitimate staffing agency is not a scam (trust.py answers that question); it is a different question: is this the employer,
or someone advertising a seat for a client? Columns:

  employer_type      direct       the job sits on the company's own board (or its verified domain) and nothing says otherwise
                     staffing     a staffing or recruitment agency placing someone with a client
                     search_firm  an executive-search or headhunting firm
                     marketplace  a reposting service or job marketplace (many copies of one ad, "jobs via ...")
                     undisclosed  the employer is not named ("Confidential", "Stealth startup")
                     unknown      not enough evidence either way (the default; most aggregator rows)
  employer_type_why  the evidence, with its confidence (high | medium)

Signals: the company name (known firms, "Staffing", "Recruiting", "Executive Search", "... Search"), the posting's own words
("our client is seeking", "on behalf of our client", "confidential client", "we are a staffing firm"), and the intel cluster
(MASS copies of one title). Nothing is hidden here: priority.py caps intermediaries at C and the tracker decides. Public data only.
"""
import re

TYPES = ("direct", "staffing", "search_firm", "marketplace", "undisclosed", "unknown")
INTERMEDIARY = ("staffing", "search_firm", "marketplace", "undisclosed")

_FIRMS = {
    "staffing": ["robert half", "accountemps", "insight global", "randstad", "adecco", "kforce", "teksystems", "aerotek",
                 "kelly services", "manpower", "manpowergroup", "vaco", "beacon hill", "addison group", "harvey nash",
                 "michael page", "page group", "pagegroup", "solomon page", "tandym", "the execu search group", "execu search",
                 "burnett specialists", "apex systems", "cybercoders", "jobot", "motion recruitment", "lhh",
                 "creative financial staffing", "signature consultants", "brooksource", "synergis", "24 seven", "hirewell",
                 "planet group", "lucas group", "phaidon international", "selby jennings", "wall street associates",
                 "clear point consultants", "careerscape", "referment", "the intersect group", "momentum resources",
                 "parker + lynch", "parker lynch", "a-line staffing", "collabera", "russell tobin", "artech", "talentburst",
                 "ledgent", "medix", "allegis", "experis", "pyramid consulting", "judge group", "the judge group",
                 "nesco resource", "aston carter", "actalent", "eliassen", "bcforward", "advantage resourcing",
                 "goodwin recruiting", "korn ferry staffing"],
    "search_firm": ["korn ferry", "heidrick", "spencer stuart", "russell reynolds", "egon zehnder", "dynamics execusearch",
                    "glocap", "smith hanley", "options group", "sterling search"],
    "marketplace": ["jobgether", "lensa", "jooble", "talent.com", "talentify", "jobs via dice", "dice", "hirebus", "hiring cafe",
                    "jobleads", "ladders", "the ladders", "efinancialcareers", "simplyhired", "careerbuilder",
                    "snagajob", "adzuna", "learn4good", "jobright", "jobs for humanity", "remotehunter", "virtual vocations",
                    "flexjobs", "dataannotation", "mercor", "outlier", "braintrust"],
}
_NAME_RX = [
    (re.compile(r"\b(staffing|recruit(?:ing|ment|ers)?|personnel|placements?|temps?|talent (?:acquisition|partners|solutions|group|"
                r"network|resources)|workforce solutions|employment (?:agency|solutions))\b", re.I), "staffing"),
    (re.compile(r"\b(executive search|search (?:group|partners|firm|associates|consultants|advisors|international)|headhunt\w*|"
                r"search)\s*(?:,?\s*(?:inc|llc|ltd|corp|co)\.?)?$", re.I), "search_firm"),
    (re.compile(r"^(?:jobs? via|hiring for|posted by)\b", re.I), "marketplace"),
]
_UNNAMED = re.compile(r"^(?:confidential(?: company| employer| client)?|stealth(?: startup| mode| company)?|undisclosed(?: company)?|"
                      r"private (?:company|client|equity firm)|anonymous|n/?a|unknown|company)$", re.I)
_HIRE = r"(?:is|are)\s+(?:currently\s+|actively\s+|now\s+)?(?:seeking|looking|hiring|searching|in search|in need|recruiting)"
# Recruitment phrasing only: consulting and banking postings say "on behalf of a client" or "confidential client" as a duty, so a
# client phrase counts only when it is the client who is hiring.
_TEXT = [
    (re.compile(r"\bour (?:client|customer),?\s+(?:an?\s+[^.]{0,90}?,?\s+)?" + _HIRE, re.I), "our client is hiring"),
    (re.compile(r"\b(?:one of our|a) (?:valued |key |premier |prestigious )?clients?,?\s+(?:an?\s+[^.]{0,90}?,?\s+)?" + _HIRE, re.I),
     "one of our clients is hiring"),
    (re.compile(r"\bon behalf of (?:our|a|one of our) (?:valued |prestigious )?client(?!s)[^.]{0,60}?\b(?:seeking|looking|hiring|search|recruit\w*|"
                r"to (?:find|fill|hire|identify))", re.I), "recruiting on behalf of a client"),
    (re.compile(r"\bconfidential (?:client|company|employer)[^.]{0,40}?\b(?:seeking|looking|hiring|search)", re.I), "a confidential client is hiring"),
    (re.compile(r"\b(?:we are|is) an? (?:leading |national |boutique |specialized |specialist |premier |award[- ]winning |global )*(?:staffing|"
                r"recruitment|recruiting|executive search|search|talent acquisition|placement) (?:firm|agency|company|partner|consultancy)", re.I),
     "describes itself as a staffing or search firm"),
    (re.compile(r"\b(?:contract[- ]to[- ]hire|temp[- ]to[- ]perm|w2 contract|c2c|corp[- ]to[- ]corp)\b", re.I), "contract-to-hire terms"),
    (re.compile(r"\b(?:direct hire|permanent placement) (?:opportunity|role|position) with\b", re.I), "a placement with another company"),
]
_NATIVE = re.compile(r"^(greenhouse|lever|ashby|workday|smartrecruiters|workable|breezy|recruitee|teamtailor|pinpoint|rippling|personio):")


def _name(company):
    c = re.sub(r"\s+", " ", str(company or "").lower().replace("&", " and ")).strip(" .,")
    c = re.sub(r",?\s*\b(inc|llc|ltd|corp|corporation|co|company|group|usa|us|na|plc)\.?$", "", c).strip(" .,")
    return c


def classify(row, mass=False):
    """(employer_type, why). mass: intel.py found MASS+ copies of one title at this employer."""
    raw = str(row.get("company") or "").strip()
    name = _name(raw)
    if not raw or _UNNAMED.match(raw):
        return "undisclosed", "high: the employer is not named"
    for kind, names in _FIRMS.items():
        for f in names:
            if name == f or name.startswith(f + " ") or (len(f) > 5 and re.search(r"\b" + re.escape(f) + r"\b", name)):
                return kind, f"high: {raw} is a known " + {"staffing": "staffing or recruiting firm", "search_firm": "search firm",
                                                                      "marketplace": "job marketplace"}[kind]
    for rx, kind in _NAME_RX:
        if rx.search(raw):
            return kind, f"high: the name says so ({raw})"
    text = str(row.get("description") or "").replace("\\n", "\n")[:12000]
    said = [why for rx, why in _TEXT if rx.search(text)]
    contract = "contract-to-hire terms" in said
    said = [s for s in said if s != "contract-to-hire terms"]
    if len(said) >= 2 or (said and contract):
        return "staffing", "high: the posting says " + "; ".join(said + (["contract-to-hire terms"] if contract else []))
    if said:
        return "staffing", "medium: the posting says " + said[0]
    if mass:
        return "marketplace", "medium: many copies of one ad under this name"
    native = bool(_NATIVE.match(str(row.get("id") or "")))
    trust = row.get("trust")
    if native and trust in ("verified", "consistent"):
        return "direct", f"high: on its own board ({trust})"
    if native:
        return "direct", "medium: on a company board under this name"
    if trust == "verified":
        return "direct", "medium: applies on the employer's verified site"
    return "unknown", ""


def annotate(rows, mass_keys=None):
    """Fills employer_type and employer_type_why. mass_keys: employer keys with MASS+ copies of one title (worked out from the
    rows when not given, as intel.py does). Returns counts by type."""
    import identity
    import intel
    if mass_keys is None:
        fam = {}
        for r in rows:
            k = (identity.emp_key(r.get("company")), identity.title_ident(r.get("title")))
            fam[k] = fam.get(k, 0) + 1
        mass_keys = {e for (e, _), n in fam.items() if e and n >= intel.MASS}
    out = {}
    for r in rows:
        try:
            t, why = classify(r, identity.emp_key(r.get("company")) in mass_keys)
        except Exception:
            t, why = "unknown", ""
        r["employer_type"], r["employer_type_why"] = t, why
        out[t] = out.get(t, 0) + 1
    return out


def is_intermediary(row):
    return row.get("employer_type") in INTERMEDIARY
