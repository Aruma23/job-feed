"""Who posted the job: the employer itself, or a recruiter or staffing firm (feed v8, Nov 9 2026).

This is a separate question from trust. trust.py asks "is this listing really from the name on it?"; this module asks "is the
name on it the company you would work for?". A staffing firm's job on its own Greenhouse board is verified (it is Vaco's
board) and is still a recruiter posting. Recruiters are legitimate and never rejected here. What changes is what you can check:
when the hiring company isn't named you cannot look it up, see whether you've already applied there, or tell whether it is a
company you must avoid, so the feed says so and ranks the job lower until you know.

Columns:

  poster       employer              no sign of a third party (the default; it is "no evidence of a recruiter", not proof)
               recruiter             a recruiter or staffing firm posted it (its name, or the employer posts for clients)
               recruiter_unnamed     a recruiter posted it and the hiring company is not named ("our client", "confidential")
  poster_why   the evidence in words

Evidence, strongest first (Textkernel's finding: pattern matching on names and wording catches most agency postings, and an
organization's postings judged together beat one posting alone):
  1. the company name is a known recruiting or staffing firm, or reads like one ("... Staffing", "... Search Partners");
  2. the posting says it is written for a client ("our client is seeking", "on behalf of our client", "confidential client
     search", "we are partnering with a ... to find");
  3. an employer whose fitting postings mostly say (2) is a recruiter for all its postings.
Wording that a consulting, law or advisory firm uses about its own clients ("serve our clients", "on behalf of our clients",
"confidential client information") is not evidence: the patterns need the client to be the one hiring.
"""
import re

import identity

# Recruiting and staffing firms that post finance roles in the NYC market under their own name. Matched on the start of the
# normalized company name, so "Robert Half International" and "Kforce Inc" match and "CFS Brands" does not.
KNOWN = (
    "robert half", "accountemps", "protiviti talent", "randstad", "adecco", "kforce", "insight global", "teksystems", "aerotek",
    "beacon hill", "vaco", "addison group", "lhh", "korn ferry", "michael page", "page executive", "hays", "manpower",
    "kelly services", "apex systems", "motion recruitment", "harnham", "selby jennings", "phaidon international", "tandym",
    "cybercoders", "jobot", "creative financial staffing", "the judge group", "judge group", "ledgent", "signature consultants",
    "atrium staffing", "mondo", "solomon page", "hunter bond", "averity", "syndicatebleu", "execu search", "wall street services",
    "walker hill", "russell tobin", "brooksource", "eliassen", "the greene group", "job mobz",
    "bullpen talent", "bullpen", "ladders", "zrg partners", "lumera search", "northbound executive", "clearbridge executive",
    "arrow search", "coda search", "dg talent", "ldk talent", "whitecap workforce", "growe talents", "networkright",
    "the career group", "options group", "glocap", "cpl", "spherion", "express employment", "yoh", "talentbridge",
    "4 corner resources",
)
_KNOWN_RX = re.compile(r"^(?:" + "|".join(re.escape(k) for k in sorted(KNOWN, key=len, reverse=True)) + r")\b")
# Name shapes that only staffing and search firms use. "Consulting Group" is deliberately absent (BCG, Brattle).
NAME = re.compile(r"\b(?:staffing|recruit(?:ing|ers?|ment)|headhunt\w*|executive search|search (?:partners|group|firm|associates)|"
                  r"talent (?:partners|solutions|group|acquisition partners)|personnel|workforce solutions|placement(?:s| services)|"
                  r"employment (?:agency|services))\b", re.I)
# The client is the one hiring. Each pattern stays inside one sentence.
_S = r"[^.\n]{0,90}?"
FOR_CLIENT = [(re.compile(rx, re.I), why) for rx, why in [
    (r"\b(?:our|my|a) client\b(?!s)" + _S + r"\b(?:is|are) (?:seeking|looking|hiring|searching|in search|in need|recruiting|adding|"
     r"growing (?:its|their) team)\b", "says its client is hiring"),
    (r"\bour client,? (?:a|an|one of)\b" + _S + r"\b(?:seek|look|hir|search|recruit)", "says its client is hiring"),
    (r"\bour client'?s\b" + _S + r"\b(?:team|group|department)\b" + _S + r"\b(?:seek|look|hir)", "says its client is hiring"),
    (r"\bon behalf of (?:our|a|an|one of our) (?:\w+ ){0,3}client\b(?!s)", "posts on behalf of a client"),
    (r"\bon behalf of one of our clients\b", "posts on behalf of a client"),
    (r"\b(?:for|with) (?:a|an|our) confidential (?:client|company|employer|organization)\b", "the hiring company is confidential"),
    (r"\bconfidential (?:search|posting|opportunity|client is)\b", "the hiring company is confidential"),
    (r"\bclient of ours\b", "says its client is hiring"),
    (r"\b(?:we are|we're|we have been|we've been) (?:exclusively )?(?:partnering|partnered|working|retained|engaged) (?:with|by) "
     r"(?:a|an|our)\b" + _S + r"\b(?:to (?:find|hire|identify|recruit|fill)|seeking|looking|search for)", "recruits for a partner company"),
    (r"\b(?:is|are) (?:seeking|looking for|hiring)" + _S + r"\bfor (?:a|an|our) client\b", "says its client is hiring"),
]]
# The client is named when the posting says who it is: "our client, Acme Corp," is rare; "our client, a ..." is the norm. The
# feed doesn't try to read the name out; a recruiter posting is "unnamed" when only client wording identifies the employer
# and the wording calls it confidential or describes it ("a leading ... firm") instead of naming it.
UNNAMED = re.compile(r"\bconfidential\b|\bour client,? (?:a|an)\b|\bour client is (?:a|an)\b|\b(?:a|an) (?:leading|growing|"
                     r"well[- ]known|prestigious|top|global|large|fast[- ]growing|venture[- ]backed|privately held|mid[- ]?size|"
                     r"publicly traded|established)\b" + _S + r"\bclient\b|\bpartnering with (?:a|an)\b", re.I)
CONTRACT = re.compile(r"\b(?:contract[- ]to[- ](?:hire|perm)|temp[- ]to[- ](?:hire|perm)|temporary assignment|w-?2 contract|"
                      r"1099 contract|(?:\d+|six|three|twelve)[- ]month contract)\b", re.I)


def name_says(company):
    """Why the company's name marks a recruiter, or ''."""
    k = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(company or "").lower().replace("&", " and ").replace("'", ""))).strip()
    if not k:
        return ""
    if _KNOWN_RX.match(k) or _KNOWN_RX.match(re.sub(r"^the ", "", k)):
        return f"{company} is a recruiting or staffing firm"
    if NAME.search(str(company)):
        return f"{company}'s name reads like a recruiting or staffing firm"
    return ""


def text_says(description):
    """(why, unnamed) when the posting says it is written for a client; ('', False) otherwise."""
    d = str(description or "").replace("\\n", "\n")[:8000]
    for rx, why in FOR_CLIENT:
        m = rx.search(d)
        if m:
            start = max(0, m.start() - 120)
            return why, bool(UNNAMED.search(d[start:m.end() + 160]))
    return "", False


def assess_all(rows):
    """Sets poster and poster_why on every row. Judges each employer's postings together: an employer whose postings mostly say
    they are written for a client is a recruiter for all of them. Returns counts. Never raises for one row."""
    seen = {}
    for r in rows:
        try:
            why, unnamed = text_says(r.get("description"))
        except Exception:
            why, unnamed = "", False
        r["_pt"] = (why, unnamed)
        k = identity.emp_key(r.get("company"))
        if k:
            n = seen.setdefault(k, [0, 0])
            n[0] += 1
            n[1] += 1 if why else 0
    counts = {}
    for r in rows:
        why, unnamed = r.pop("_pt", ("", False))
        k = identity.emp_key(r.get("company"))
        total, client = seen.get(k, (0, 0))
        named = name_says(r.get("company"))
        reasons = []
        if named:
            reasons.append(named)
        if why:
            reasons.append("the posting " + why)
        elif client >= 2 and client * 2 >= total:
            reasons.append(f"{client} of {total} of this employer's postings say they are for a client")
        if not reasons:
            r["poster"], r["poster_why"] = "employer", ""
        else:
            r["poster"] = "recruiter_unnamed" if why and unnamed else "recruiter"
            if r["poster"] == "recruiter_unnamed":
                reasons.append("the hiring company is not named: find out who it is before applying")
            if CONTRACT.search(str(r.get("description") or "")[:8000]) or re.search(r"\bcontract\b|\btemp\b", str(r.get("title") or ""), re.I):
                reasons.append("a contract or temp-to-hire seat")
            r["poster_why"] = "; ".join(reasons)
        counts[r["poster"]] = counts.get(r["poster"], 0) + 1
    return counts
