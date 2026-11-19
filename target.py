"""The target profile: who the feed is for, in one place (Nov 18 2026).

Before this file the target lived in five places that had drifted apart: the model's prompt said "analyst, senior analyst or
associate", the scheduler ran "finance and strategy manager" searches, the boards kept manager titles, the ranking took 6
points off for manager level, and a third of what was published was manager level or above. Every test passed, because each
part did what it had been told. Now every part reads the level and the experience reach from here, tests/test_target.py
fails when one of them disagrees, and each run counts what it publishes against this profile (feed_status "target").

To change who the feed is for, change this file. Nothing else needs to know.
"""
import re

import reqs

LEVEL_OUT = ("manager", "director+", "intern")   # reqs.requirements()["seniority"] values that are never published
MAX_REQUIRED_YEARS = 4                           # a posting that REQUIRES more years than this is not published; a figure that
                                                 # is only preferred never counts (reqs.years_required)
PROMPT_LEVEL = "analyst, senior analyst or associate roles"           # the words the review model is given for the level
# Title words the company boards skip before fetching a description (discover.HARD_EXCLUDE adds them).
TITLE_WORDS_OUT = ["manager", "managers", "mgr", "supervisor", "avp", "assistant vice president", "team lead"]
# A search phrase that asks for these is retired (scheduler.plan) and never admitted.
SEARCH_OUT = re.compile(r"\b(manager|director|head of|vice president|vp|avp|supervisor|lead|intern|internship|investment banking|"
                        r"equity research|trading|private equity|credit analyst|underwriting|valuation|contract|temporary|investment analyst|"
                        r"portfolio analyst)\b", re.I)


# A posting that requires a language other than English (in its title, or in a sentence that says fluent / bilingual / required
# without "preferred" or "a plus"). English is the profile's working language; a bilingual-required role is a certain reject.
_LANG = (r"(mandarin|chinese|cantonese|japanese|korean|spanish|french|german|portuguese|italian|hebrew|arabic|russian|hindi|"
         r"vietnamese|turkish|polish|dutch)")
_LANG_TITLE = re.compile(r"\b" + _LANG + r"\b[^,;]{0,12}\b(bilingual|speak\w*|fluen\w*)|\bbilingual\b[^,;]{0,12}\b" + _LANG + r"\b", re.I)
_LANG_TEXT = re.compile(r"\b(?:bilingual|fluen\w+|native)\b[^.;\n]{0,30}\b" + _LANG + r"\b[^.;\n]{0,60}|\b" + _LANG +
                        r"\b[^.;\n]{0,30}\b(?:bilingual|fluen\w+)\b[^.;\n]{0,60}", re.I)
_LANG_SOFT = re.compile(r"prefer|a plus|plus\b|bonus|nice to have|desir|advantag|asset\b|helpful|ideal|not required|optional|benefi|"
                        r"welcome|\bor english", re.I)


def language_required(row):
    """The language a posting requires besides English, or ''."""
    m = _LANG_TITLE.search(str(row.get("title") or ""))
    if m:
        return next(g for g in m.groups() if g and re.fullmatch(_LANG, g, re.I)).title()
    for m in _LANG_TEXT.finditer(str(row.get("description") or "").replace("\\n", "\n")[:12000]):
        if not _LANG_SOFT.search(m.group(0)):
            return next(g for g in m.groups() if g).title()
    return ""


# Lanes outside the profile (decided Nov 18: "do what makes sense"). Markets and deal roles are not planning or corporate
# finance and hire from their own analyst programs: investment banking, trading, equity / investment / credit research,
# private equity and venture investing, M&A and deal advisory, capital markets, underwriting and credit, valuation,
# securities and structured products, quant, actuarial. A title that is plainly a finance-team job (FP&A, financial analyst,
# treasury, strategic / corporate finance) stays even when it names such a business ("FP&A Analyst, Capital Markets").
MARKETS = re.compile(
    r"investment bank\w*|\b(investment|corporate) (&|and) (corporate|investment) banking\b|\bcorporate banking\b|\bequity research\b|"
    r"\b(global )?investment research\b|\bcredit research\b|\bsell[- ]side\b|\bbuy[- ]side\b|\btrad(er|ing)\b|\bprivate equity\b|"
    r"\bventure capital\b|\bhedge fund\b|\bm&a\b|\bmergers\b|\bleveraged finance\b|\bcapital markets\b|\bfinancial markets\b|"
    r"\bunderwrit\w*|\bportfolio manage\w*|\bwealth\b|\bcredit (analyst|risk|specialist|officer|associate|review)\b|\bdeals\b|"
    r"\bdeal advisory\b|\btransaction (advisory|services)\b|\bvaluations?\b|\bsecurit(ies|ized)\b|\bstructured\b|\bfixed income\b|"
    r"\bderivativ\w*|\bactuar\w*|\binvestment (analyst|associate|professional|officer|strategist)\b|"
    r"\b(analyst|associate),? investments?\b|\bquantitative (research|trading|analyst|finance)\b|\bquant\b|\bloan (origination|officer)\b|"
    r"\bbanker\b|\bbrokerage\b|\basset[- ]backed\b", re.I)
FINANCE_TEAM = re.compile(r"fp&a|financial planning|planning|budget|forecast|corporate finance|strategic finance|business finance|"
                          r"finance business partner|treasury|financial analyst|finance analyst|finance (&|and) strategy", re.I)
# Not a permanent full-time job: said in the title, or in a sentence that states the terms (not one that merely uses the word:
# "contract negotiations", "non-temporary position").
TERMS_TITLE = re.compile(r"\(contract\w*\)|[-–,:] *contract\w*\b|^contract\b(?!s| (analyst|administrat\w+|specialist|manage\w*|coordinator|negotiat\w+|associate|compliance|operations))|\bcontract\s*$|\bcontract(-| )to(-| )hire\b|"
                         r"\bcontract (role|position)\b|\b\d+[- ]months?\b|\btemp(orary)?\b|\bpart[- ]time\b|\binterim\b|\bfreelance\w*\b|"
                         r"\bseasonal\b|\bper diem\b|\bhours?( per|/) ?week\b|\b\d+ ?(hours?|hrs?)\b|\bpt\b", re.I)
TERMS_TEXT = re.compile(r"\bthis (is|role is|position is) an? [^.\n]{0,40}\b(contract|temporary|part[- ]time)\b[^.\n]{0,20}\b(position|role|"
                        r"assignment|opportunity|engagement)\b|\bcontract[- ]to[- ]hire\b|\bfor this contract (role|position)\b|"
                        r"\b\d+[-–to ]*\d*[- ]months? (contract|assignment)\b|\b(contract |project |assignment )?duration: *\d+", re.I)


def lane(row):
    """'' or why the job is outside the lanes and terms the profile wants: 'lane: markets role', 'terms: contract'."""
    t = str(row.get("title") or "")
    if MARKETS.search(t) and not FINANCE_TEAM.search(t):
        return "lane: markets or deals role"
    if TERMS_TITLE.search(t) or TERMS_TEXT.search(str(row.get("description") or "").replace("\\n", "\n")[:8000]):
        return "terms: contract, temporary or part-time"
    return ""


def off(row, fresh=False):
    """'' when the job is inside the target, else why not: 'level: manager', 'years: requires 6+', 'language: requires Korean',
    'lane: markets or deals role' or 'terms: contract, temporary or part-time'.
    fresh: ignore a stored seniority label (a row from an earlier feed was labelled by older patterns)."""
    lvl = (not fresh and row.get("seniority")) or reqs.requirements(row.get("title"), row.get("description"))["seniority"]
    if lvl in LEVEL_OUT:
        return f"level: {lvl}"
    lo, _ = reqs.years_required(str(row.get("description") or "").replace("\\n", "\n")[:12000])
    if lo > MAX_REQUIRED_YEARS:
        return f"years: requires {lo}+"
    lang = language_required(row)
    if lang:
        return f"language: requires {lang}"
    return lane(row)


def off_safe(row, fresh=False):
    try:
        return off(row, fresh)
    except Exception:
        return ""


def check(rows):
    """What a finished feed looks like against the profile: violations must be 0 (a row here means a path skipped the gate);
    reach is the share that sits at the edge of the profile (senior level, or the most years still allowed)."""
    bad = [(r, off_safe(r)) for r in rows]
    bad = [(r, why) for r, why in bad if why]
    edge = sum(1 for r in rows if r.get("seniority") == "senior" or
               reqs.years_required(str(r.get("description") or "").replace("\\n", "\n")[:12000])[0] == MAX_REQUIRED_YEARS)
    return {"levels_out": list(LEVEL_OUT), "max_required_years": MAX_REQUIRED_YEARS, "published": len(rows),
            "violations": len(bad), "examples": [f"{r.get('company')}: {r.get('title')} ({why})" for r, why in bad[:5]],
            "reach_share": round(edge / max(1, len(rows)), 3)}
