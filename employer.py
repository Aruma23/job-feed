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
import hashlib
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
                 "goodwin recruiting", "korn ferry staffing", "clark davis associates", "hire resolve",
                 "hire resolve.com"],
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
_NATIVE = re.compile(r"^(greenhouse|lever|ashby|workday|smartrecruiters|workable|breezy|recruitee|teamtailor|pinpoint|rippling|personio|oracle):")


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
    native = bool(_NATIVE.match(str(row.get("id") or "")))
    trust = row.get("trust")
    if mass and not (native and trust == "verified"):   # the employer's own website links this board: its copies are its own
        return "marketplace", "medium: many copies of one ad under this name"
    other = re.search(r"applies through (.{1,60})'s board \(a recruiter or parent company\?\)", str(row.get("trust_why") or ""))
    if other:                                        # trust.py: the board belongs to another company. Parent or recruiter can't
        return "unknown", f"medium: applies through {other.group(1)}'s board (parent company or recruiter)"   # be told apart
    if native and trust in ("verified", "consistent"):
        return "direct", f"high: on its own board ({trust})"
    if native and trust == "suspicious":
        return "unknown", "high: the application route is marked suspicious"
    if native:
        return "unknown", "medium: company-board ownership is not independently linked to the named employer"
    if trust == "verified":
        return "direct", "medium: applies on the employer's verified site"
    return "unknown", ""


def _mass_key(row):
    """Only call a repeated posting a marketplace when its substantive description is also repeated."""
    import identity
    description = re.sub(r"[^a-z0-9]+", " ", str(row.get("description") or "").lower()).strip()
    if len(description) < 200:
        return None
    digest = hashlib.sha1(description[:6000].encode()).hexdigest()[:12]
    employer_key = identity.emp_key(row.get("company"))
    title_key = identity.title_ident(row.get("title"))
    return (employer_key, title_key, digest) if employer_key and title_key else None


def annotate(rows, mass_keys=None):
    """Fills employer_type and employer_type_why. Returns counts by type.

    mass_keys: what counts as mass posting, worked out from the rows when not given. Two signals, either is enough (Nov 14 review
    of the deep-audit PRs #3-#5): MASS+ copies of one title at one employer (the v10 rule: on the Nov 15 00:10 UTC feed it caught
    Clark Davis Associates, a recruiting firm, 47 rows on its own SmartRecruiters board with a different text per ad, and an
    insurance agency's 40 city copies of one sales ad), or MASS+ copies of one substantive description (the PR's rule: the same
    ad under several titles). The PR replaced the first with the second; on the real feed that fixed no misread employer and
    lifted 12 of the firm's ads into A/B, so both are kept. A real employer misread by the title rule shows in the audit's
    marketplace counts; exempt it on evidence."""
    import identity
    import intel
    if mass_keys is None:
        titles, ads = {}, {}
        for r in rows:
            e, t = identity.emp_key(r.get("company")), identity.title_ident(r.get("title"))
            if e and t:
                titles[(e, t)] = titles.get((e, t), 0) + 1
            k = _mass_key(r)
            if k:
                ads[k] = ads.get(k, 0) + 1
        # Nov 18: the title signal is kept per (employer, title), not per employer. It used to mark every row of an employer
        # that posted one title MASS+ times, so a bank's eight city copies of "Financial Analyst" would have held its
        # separate FP&A Manager seat at C as a "marketplace" listing. Recruiters are still caught by name, by their own
        # words and by the repeated cluster itself.
        mass_keys = {k for k, n in titles.items() if n >= intel.MASS} | {k for k, n in ads.items() if n >= intel.MASS}
    out = {}
    for r in rows:
        try:
            e, t = identity.emp_key(r.get("company")), identity.title_ident(r.get("title"))
            mass = (e, t) in mass_keys or e in mass_keys or (_mass_key(r) or ()) in mass_keys   # e: an employer-level key given by a caller
            t, why = classify(r, mass)
        except Exception:
            t, why = "unknown", ""
        r["employer_type"], r["employer_type_why"] = t, why
        out[t] = out.get(t, 0) + 1
    return out


def is_intermediary(row):
    return row.get("employer_type") in INTERMEDIARY
