"""Employer verification and scam screening (feed v6, Nov 1 2026).

A board that returns jobs is not proof the board belongs to the employer, and an aggregator's "apply" link can lead anywhere.
Greenhouse, Workday and the rest host many employers, so the platform's domain proves nothing; what counts is the link between
the employer and the specific tenant or domain. Columns:

  trust       verified    the employer's own website links to this board, or the job sits on the employer's own domain,
                          or the domain is confirmed good in domains.txt
              consistent  the board's name matches the employer, or an aggregator listing for this employer applies through it
              unverified  nothing confirms it either way (the default for anything unknown: never a rejection)
              suspicious  applicant-directed scam wording, a lookalike or chat-app apply link, or a domain confirmed bad
  trust_why   the evidence in words

Evidence is remembered in boards.json: "employers" (official domain, when it was checked, which boards it links to) and
"domains" (every apply domain seen, with its verdict). domains.txt holds confirmed verdicts ("good blackrock.com", "bad
blackrock-careers.net fake recruiter"). The official-site check is budgeted (VERIFY_BUDGET employers a run, two pages each,
rechecked monthly) and only runs for employers with a qualifying job.
"""
import re
import unicodedata
import urllib.parse
from datetime import date

VERIFY_BUDGET = 15
RECHECK_DAYS = 30
OVERRIDES = "domains.txt"

# Third-party hiring platforms and job-distribution services: legitimate hosts that prove nothing about the employer by
# themselves.
PLATFORM = re.compile(
    r"(?:^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|myworkdayjobs\.com|myworkdaysite\.com|workable\.com|"
    r"breezy\.hr|recruitee\.com|teamtailor\.com|pinpointhq\.com|rippling\.com|personio\.(?:de|com)|icims\.com|taleo\.net|"
    r"successfactors\.(?:com|eu)|sapsf\.com|oraclecloud\.com|jobvite\.com|bamboohr\.com|dayforcehcm\.com|ultipro\.com|ukg\.com|"
    r"paylocity\.com|adp\.com|avature\.net|silkroad\.com|applytojob\.com|jazzhr\.com|hrmdirect\.com|recruiterbox\.com|"
    r"saashr\.com|ongig\.com|jometer\.com|contacthr\.com|phenompeople\.com|eightfold\.ai|clearcompany\.com|paycomonline\.net|"
    r"paycor\.com|isolvedhire\.com|appcast\.io|recruitics\.com|cityjobs\.nyc\.gov|usajobs\.gov|jobs\.net|careerplug\.com|"
    r"hiringthing\.com|workforcenow\.adp\.com|brassring\.com|kenexa\.com|cornerstoneondemand\.com|csod\.com|myjobs\.adp\.com|"
    r"theapplicantmanager\.com|applicantpro\.com|zohorecruit\.com|freshteam\.com|gem\.com|dover\.com|wellfound\.com|"
    r"builtin\.com|efinancialcareers\.com|jcp\.com|un\.org)$", re.I)
AGGREGATOR = re.compile(r"(?:^|\.)(indeed|linkedin|glassdoor|ziprecruiter|google)\.", re.I)
CHAT_OR_FORM = re.compile(r"^(?:wa\.me|api\.whatsapp\.com|chat\.whatsapp\.com|t\.me|telegram\.me|forms\.gle|docs\.google\.com|"
                          r"forms\.office\.com|signal\.me|discord\.gg|jotform\.com|form\.jotform\.com|typeform\.com|"
                          r"[a-z0-9-]+\.typeform\.com)$", re.I)
SHORTENER = re.compile(r"^(?:bit\.ly|tinyurl\.com|t\.co|ow\.ly|rebrand\.ly|cutt\.ly|is\.gd|shorturl\.at)$", re.I)
CHEAP_TLD = re.compile(r"\.(?:xyz|top|online|site|info|icu|live|click|shop|work|buzz|cyou|rest|lol|monster|space|website|"
                       r"fun|store|link|support|cfd|sbs)$", re.I)
GENERIC = {"financial", "finance", "capital", "partners", "management", "bank", "health", "healthcare", "services", "group",
           "solutions", "technologies", "technology", "systems", "media", "network", "america", "american", "new", "york",
           "city", "national", "united", "general", "first", "global", "international", "consulting", "advisors", "insurance",
           "investments", "investment", "asset", "holdings", "company", "brands", "labs", "studio", "careers", "jobs"}
# Scam wording aimed at the applicant (not a job duty like "process wire transfers" or a product like WhatsApp).
SEVERE = [(re.compile(rx, re.I), why) for rx, why in [
    (r"\b(?:interview|chat|contact|reach|message|text)\w*\b[^.\n]{0,40}\b(?:on|via|through|using|over)\s+(?:telegram|whatsapp|"
     r"signal app|wickr|google hangouts|hangouts)\b", "interview or contact through a chat app"),
    (r"\b(?:telegram|whatsapp)\s*(?:id|number|handle|contact)?\s*[:@]\s*\+?\w", "a chat-app handle to contact"),
    (r"\b(?:you will|you'll|we will|we'll)\s+(?:receive|be sent|send you|mail you)\s+(?:a|an)?\s*(?:check|cheque)\b",
     "promises to send the applicant a check"),
    (r"\b(?:deposit|cash)\s+(?:the|a|this|your)\s+(?:check|cheque)\b", "asks the applicant to deposit a check"),
    (r"\b(?:purchase|buy)\s+(?:the\s+|your\s+own\s+)?(?:equipment|software|gift cards?|supplies|laptop)\b[^.\n]{0,80}"
     r"\b(?:reimburs|refund|we will send|we'll send)", "asks the applicant to buy equipment for reimbursement"),
    (r"\b(?:pay|send|transfer)\s+(?:us\s+)?(?:a|an|the)?\s*(?:refundable\s+)?(?:fee|deposit|payment)\s+(?:for|to cover)\s+"
     r"(?:the\s+|your\s+)?(?:training|equipment|onboarding|background check|application|processing|starter kit)",
     "asks the applicant to pay a fee"),
    (r"\b(?:training|application|registration|onboarding|starter kit|processing)\s+fee\s+(?:of|is)\s*\$", "charges an applicant fee"),
    (r"\b(?:provide|send|submit|share)\s+(?:us\s+)?(?:your\s+)?(?:bank(?:ing)?\s+(?:account|details|information|login)|"
     r"routing number|credit card|debit card)\b[^.\n]{0,50}\b(?:to apply|to be considered|before (?:the|an|your) interview|"
     r"with your (?:resume|application)|for (?:the|your) (?:interview|application))", "asks for bank details to apply"),
    (r"\b(?:send|text|provide|submit|share)\s+(?:us\s+)?(?:your\s+)?(?:ssn|social security(?: number)?|"
     r"driver'?s licen[cs]e (?:number|photo|picture))\b[^.\n]{0,40}\b(?:to apply|with your (?:resume|application)|"
     r"before (?:the|an|your) interview|to be considered)", "asks for an SSN or license to apply"),
    (r"\bno interview (?:required|needed|necessary)\b", "hires with no interview"),
]]
SOFT = [(re.compile(rx, re.I), why) for rx, why in [
    (r"\b(?:send|email|submit|forward)\s+(?:your\s+)?(?:resume|cv|application)\b[^.\n]{0,60}@(?:gmail|yahoo|outlook|hotmail|aol|"
     r"proton(?:mail)?|icloud)\.com", "applications go to a free email account"),
    (r"\b(?:earn|make)\s+(?:up to\s+)?\$\s?\d[\d,]{2,}\s*(?:per|a|/)\s*(?:day|week)\b", "pay quoted per day or week"),
    (r"\b(?:crypto(?:currency)?|bitcoin|usdt)\s+(?:wallet|payment|transfer)s?\b", "crypto payments"),
    (r"\b(?:urgent(?:ly)? hiring|immediate start)\b[^.\n]{0,60}\b(?:no experience|any experience)", "urgent hire, no experience"),
]]


def ascii_lower(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def emp_tokens(company):
    """(compact, distinctive words): 'JPMorgan Chase & Co.' -> ('jpmorganchase', {'jpmorgan', 'chase'})."""
    import identity
    k = identity.emp_key(ascii_lower(company))
    words = {w for w in k.split() if len(w) >= 4 and w not in GENERIC}
    return k.replace(" ", ""), words


def host_of(url):
    try:
        h = (urllib.parse.urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def registered(host):
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def short_names(company):
    """Exact-only short forms: the first word and the initials ('TD Bank' -> td, tb; 'Neuberger Berman' -> neuberger, nb)."""
    import identity
    w = identity.emp_key(ascii_lower(str(company or "").replace("&", " ").replace(" and ", " "))).split()
    return {x for x in (w[0] if w else "", "".join(x[0] for x in w) if len(w) > 1 else "") if len(x) >= 2}


def name_match(label, compact, words, alias=()):
    """Does a domain label or board slug name this employer? Exact, prefix either way (4+ letters), a distinctive word, or
    exactly its first word or initials."""
    label = re.sub(r"[^a-z0-9]", "", ascii_lower(label))
    if not label or not compact:
        return False
    if label in alias:
        return True
    if label == compact or (len(label) >= 4 and compact.startswith(label)) or (len(compact) >= 4 and label.startswith(compact)):
        return True
    return any(w in label for w in words)


def load_overrides(path=OVERRIDES):
    good, bad = {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                p = ln.split(None, 2)
                if len(p) >= 2 and not ln.startswith("#"):
                    (good if p[0].lower() == "good" else bad if p[0].lower() == "bad" else {})[p[1].lower()] = (p[2].strip() if len(p) > 2 else "")
    except FileNotFoundError:
        pass
    return good, bad


NEGATED = re.compile(r"\b(?:never|not|won'?t|don'?t|doesn'?t|no one|nobody|beware|scam|scams|fraud|fraudulent|phishing|impersonat\w*|"
                     r"warning|caution|be aware|legitimate)\b", re.I)


def _said(rx, t):
    """A match that isn't inside a warning ("we will never interview via Telegram"): employers' anti-scam notices name the
    same tricks scammers use."""
    for m in rx.finditer(t):
        before = re.split(r"[.!?\n]", t[max(0, m.start() - 160):m.start()])[-1]
        if not NEGATED.search(before + " " + m.group(0)):
            return True
    return False


def scam_signals(text):
    t = str(text or "").replace("\\n", "\n")
    severe = [why for rx, why in SEVERE if _said(rx, t)]
    soft = [why for rx, why in SOFT if _said(rx, t)]
    return severe, soft


def domain_verdict(host, company, known_domains, good, bad):
    """(status, why) for an apply link's host given the employer: employer domain, platform, lookalike, chat/form, unknown."""
    if not host:
        return "none", "no employer apply link"
    reg = registered(host)
    if reg in bad or host in bad:
        return "suspicious", f"{reg} is listed as fraudulent" + (f" ({bad.get(reg) or bad.get(host)})" if bad.get(reg) or bad.get(host) else "")
    if reg in good or host in good:
        return "employer", f"{reg} confirmed good"
    if CHAT_OR_FORM.match(host):
        return "suspicious", f"applies through {host} (chat app or form)"
    if SHORTENER.match(host):
        return "unknown", f"apply link is a shortened link ({host})"
    if PLATFORM.search(host):
        return "platform", f"applies through {reg}"
    compact, words = emp_tokens(company)
    raw = reg.split(".")[0]
    label = re.sub(r"[^a-z0-9]", "", raw)
    if reg in known_domains:
        return "employer", f"on {reg}, the employer's own domain"
    if label == compact or (words and label in words) or (len(label) >= 5 and compact.startswith(label)):
        if CHEAP_TLD.search(reg):
            return "suspicious", f"{reg} uses the employer's name on a throwaway domain"
        return "employer", f"on {reg}, the employer's own domain"
    if name_match(label, compact, words) or any(name_match(p, compact, words) for p in host.split(".")[:-2]):
        if "-" in raw or CHEAP_TLD.search(reg) or not name_match(label, compact, words):
            return "suspicious", f"{host} looks like the employer's name on someone else's domain"
        return "unknown", f"{reg} carries the employer's name but isn't confirmed as theirs"
    return "unknown", f"applies through {reg}, not confirmed as the employer's"


def board_index(db):
    """(kind, employer) -> board, for company-board rows whose links sit on the employer's own site."""
    out = {}
    for b, info in db.get("boards", {}).items():
        out.setdefault((b.split(":")[0], emp_tokens(info.get("company"))[0]), b)
    return out


def assess(row, db, good, bad, today, index=None):
    """Fills trust and trust_why on a row."""
    import ats
    boards = db.get("boards", {})
    emps = db.get("employers", {})
    company = row.get("company") or ""
    compact, words = emp_tokens(company)
    rec = emps.get(compact) or {}
    known = set(rec.get("domains", []))
    why, status = [], "unverified"
    severe, soft = scam_signals(row.get("description"))
    link = row.get("job_url_direct") or ("" if AGGREGATOR.search(host_of(row.get("job_url"))) else row.get("job_url"))
    host = host_of(link)
    b = row.get("_board") or ats.board_of(link)
    kind = str(row.get("id") or "").split(":")[0] if ":" in str(row.get("id") or "") else ""
    if not b and kind and index:
        b = index.get((kind, compact), "")
    if b:
        info = boards.get(b) or {}
        slug = b.split(":", 1)[1].split("|")[0]
        v, w = domain_verdict(host, company, known, good, bad) if host and not ats.board_of(link) else ("", "")
        if b in rec.get("linked", []):
            status = "verified"
            why.append(f"{rec.get('domain')} links to this board")
        elif v == "employer":
            status = "verified"
            why.append(f"the board publishes this job {w}")
        elif v == "unknown" and "carries the employer's name" in w and kind:
            status = "consistent"
            why.append(f"the board publishes this job on {registered(host)}, which carries the employer's name")
        elif name_match(slug, compact, words, short_names(company)) or (
                emp_tokens(info.get("company", ""))[0] == compact and name_match(slug, compact, words, short_names(info.get("company")))):
            status = "consistent"
            why.append(f"board '{slug}' matches the employer's name")
        elif info.get("src") == "feed-link":
            status = "consistent"
            why.append("an aggregator listing for this employer applies through this board")
        elif info.get("company") and emp_tokens(info.get("company"))[0] != compact and not name_match(info.get("company"), compact, words):
            why.append(f"applies through {info.get('company')}'s board (a recruiter or parent company?)")
        else:
            why.append(f"board '{slug}' doesn't name the employer")
        if v == "suspicious":
            status = "suspicious"
            why.insert(0, w)
        if rec.get("other") and b not in rec.get("linked", []) and status != "verified":
            why.append(f"{rec.get('domain')} links to a different board")
    else:
        v, w = domain_verdict(host, company, known, good, bad)
        why.append(w)
        status = {"employer": "verified", "suspicious": "suspicious"}.get(v, "unverified")
    if host and not b:
        reg = registered(host)
        if reg in bad or host in bad:
            status = "suspicious"
    if severe or len(soft) >= 2:
        status = "suspicious"
        why = severe + soft + why
    elif soft:
        why.append(soft[0])
    row.update(trust=status, trust_why="; ".join(dict.fromkeys(why))[:300])
    return row


def learn_domains(db, rows, today, overrides=OVERRIDES):
    """Remembers each employer's own domains (apply links on a domain named after them) and every apply domain's verdict."""
    emps = db.setdefault("employers", {})
    doms = db.setdefault("domains", {})
    good, bad = load_overrides(overrides)
    for r in rows:
        link = r.get("job_url_direct") or ""
        host = host_of(link)
        if not host or AGGREGATOR.search(host):
            continue
        compact, words = emp_tokens(r.get("company"))
        if not compact:
            continue
        v, w = domain_verdict(host, r.get("company"), set((emps.get(compact) or {}).get("domains", [])), good, bad)
        reg = registered(host)
        d = doms.setdefault(reg, {"first": today})
        d.update(last=today, verdict=v, emp=compact if v in ("employer", "suspicious") else d.get("emp", ""))
        if v == "employer":
            e = emps.setdefault(compact, {"name": r.get("company")})
            if reg not in e.setdefault("domains", []):
                e["domains"] = (e["domains"] + [reg])[-4:]
    # forget domains not seen for 120 days, so the record stays small
    for k in [k for k, d in doms.items() if (date.fromisoformat(today) - date.fromisoformat(d.get("last", today))).days > 120]:
        del doms[k]


def _markers(board):
    kind, _, rest = board.partition(":")
    slug = re.escape(rest.split("|")[0])
    if kind == "workday":
        t, wd, _site = rest.split("|")
        return re.compile(re.escape(f"{t}.{wd}.myworkdayjobs.com"), re.I)
    return re.compile({"greenhouse": r"greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?" + slug + r"\b|[?&]for=" + slug + r"\b",
                       "lever": r"lever\.co/" + slug + r"\b", "ashby": r"ashbyhq\.com/" + slug + r"\b",
                       "smartrecruiters": r"smartrecruiters\.com/" + slug + r"\b", "workable": r"workable\.com/" + slug + r"\b",
                       "rippling": r"rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?" + slug + r"\b", "personio": slug + r"\.jobs\.personio",
                       }.get(kind, slug + r"\.(?:breezy\.hr|recruitee\.com|teamtailor\.com|pinpointhq\.com)"), re.I)


CAREERS_HREF = re.compile(r"""href=["']([^"'#]*(?:career|jobs|join-us|work-with-us|opportunities)[^"']*)["']""", re.I)


def verify_employers(db, rows, today, stats, get, budget=VERIFY_BUDGET, qualified=lambda r: True):
    """For employers with a qualifying job on a board, opens their own website (home page, then its careers link) and records
    whether it links to that board. Budgeted and rechecked monthly. Never raises."""
    import ats
    emps = db.setdefault("employers", {})
    want = {}
    for r in rows:
        b = ats.board_of(r.get("job_url_direct") or r.get("job_url"))
        if not b or not qualified(r):
            continue
        compact, _ = emp_tokens(r.get("company"))
        if compact:
            want.setdefault(compact, (r.get("company"), set()))[1].add(b)
    done = 0
    d0 = date.fromisoformat(today)
    for compact, (name, bset) in sorted(want.items(), key=lambda kv: -len(kv[1][1])):
        e = emps.setdefault(compact, {"name": name})
        if e.get("checked") and (d0 - date.fromisoformat(e["checked"])).days < RECHECK_DAYS and set(e.get("tried", [])) >= bset:
            continue
        if done >= budget:
            break
        done += 1
        domain = (e.get("domains") or [compact + ".com"])[0]
        pages, found = [], set()
        try:
            home = get(f"https://{domain}/", want_json=False, tries=1, timeout=12) or ""
            pages.append(home)
            hrefs = CAREERS_HREF.findall(home)
            found |= {ats.board_of(h) for h in hrefs} - {""}
            own = next((h for h in hrefs if not ats.board_of(h) and (not h.startswith("http") or host_of(h).endswith(domain))), "")
            if own:
                pages.append(get(urllib.parse.urljoin(f"https://{domain}/", own), want_json=False, tries=1, timeout=12) or "")
        except Exception as ex:
            e.update(checked=today, domain=domain, result=f"unreachable ({type(ex).__name__})", tried=sorted(bset))
            continue
        html = " ".join(p or "" for p in pages)
        for b in bset:
            if _markers(b).search(html):
                found.add(b)
        others = {ats.board_of(h) for h in re.findall(r"""https?://[^"'\s<>]+""", html)} - {""} - found
        e.update(checked=today, domain=domain, tried=sorted(bset), linked=sorted(found & bset | set(e.get("linked", [])) & bset),
                 other=sorted(others)[:3], result="links to the board" if found & bset else "no board link found")
        if found & bset and domain not in e.setdefault("domains", []):
            e["domains"].insert(0, domain)
    stats["employers_checked"] = done
    stats["employers_verified"] = sum(1 for e in emps.values() if e.get("linked"))
    return done
