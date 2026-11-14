"""Which 6,000 characters of a long posting the feed keeps (feed v10, Nov 14 2026).

Before: the first DESC_CAP characters, whatever they were. 58% of rows reached the cap, and postings put the requirements
(years asked, degree, licenses) last, after benefits and EEO text, so the gate couldn't see what the job asks and stretch roles
looked like fits. Now, for a posting over the cap:
  - the first KEEP characters stay exactly as they were: the model-verdict digest (relevance.digest, first 4,000), the twin key
    (first 3,000 normalized) and the history fingerprint (first 3,000 normalized) don't change, so no cached verdict is lost and
    no "description edited" event fires;
  - the rest of the room goes to the later lines that carry requirements, pay or work arrangements (and to everything inside a
    Qualifications / Requirements section), then to ordinary lines; boilerplate (EEO, accommodations, benefits, privacy,
    E-Verify, and their headings) is never kept past the head; skipped stretches are marked "[...]";
  - nothing is rewritten: every kept line is the posting's own text, in its own order.
Postings within the cap are untouched.
"""
import re

KEEP = 4000
MARK = " [...] "
STATS = {"over_cap": 0, "years_recovered": 0, "chars_boilerplate_dropped": 0}

_SEP = re.compile(r"(\\n|\n)")
_REQ_HEAD = re.compile(r"^\W*(?:basic |minimum |required |preferred |key |your )?(?:qualifications|requirements|skills|experience|"
                       r"what you(?:'|’)?ll (?:need|bring)|what you bring|what we(?:'|’)?re looking for|who you are|you have|"
                       r"about you|must haves?|nice to haves?|education)\b", re.I)
_END_HEAD = re.compile(r"^\W*(?:benefits|perks|what we offer|why (?:join|work)|about (?:us|the company|our company|the firm)\b|"
                       r"our (?:culture|values|commitment)|equal (?:employment )?opportunity|eeo\b|diversity)", re.I)
_SIGNAL = re.compile(r"\$\s?\d|\d+\+?\s*(?:-|–|to)?\s*\d*\s*years?|\byrs?\b|required|requirement|qualif|degree|bachelor|master|mba\b|"
                     r"\bcpa\b|\bcfa\b|certif|licens|salary|pay range|compensation|base pay|hybrid|remote|on[- ]?site|in[- ]office|"
                     r"days? (?:a|per) week|location|sponsor|authori[sz]", re.I)
_BOILER = re.compile(r"equal (?:employment )?opportunity|eeo\b|affirmative action|reasonable accommodation|without regard to|"
                     r"protected veteran|disabilit|genetic information|sexual orientation|gender identity|national origin|e-verify|"
                     r"privacy (?:notice|policy)|benefits (?:include|package)|401\(?k\)?|paid time off|\bpto\b|parental leave|"
                     r"wellness|tuition|commuter|pet insurance|fertility|employee assistance|recruitment fraud|"
                     r"(?:we are|is) (?:an|a) (?:equal|proud)", re.I)
_YEARS = re.compile(r"\d+\+?\s*(?:-|–|to)?\s*\d*\s*years?", re.I)


def _chunks(text):
    parts = _SEP.split(text)
    out, i = [], 0
    for k in range(0, len(parts), 2):
        line = parts[k] + (parts[k + 1] if k + 1 < len(parts) else "")
        out.append((i, line))
        i += len(line)
    return out


def fit(text, cap):
    """The posting cut to cap characters, keeping what the gate needs. Unchanged when it already fits."""
    text = str(text or "")
    if len(text) <= cap:
        return text
    STATS["over_cap"] += 1
    keep = min(KEEP, cap)
    head = text[:keep]                                     # exactly: relevance.digest reads the first 4,000 characters
    rest = _chunks(text[len(head):])
    in_req, scored = False, []
    for pos, line in rest:
        bare = _SEP.sub("", line).strip()
        if _REQ_HEAD.match(bare) and len(bare) < 80:
            in_req = True
        elif _END_HEAD.match(bare) and len(bare) < 80:
            in_req = False
        if pos == 0:
            score = 3                                      # the rest of the line the head cut through
        elif not bare:
            score = -1
        elif (_BOILER.search(bare) or (_END_HEAD.match(bare) and len(bare) < 80)) and not _SIGNAL.search(bare):
            score = 0                                      # boilerplate: never kept past the head
        elif in_req or _SIGNAL.search(bare):
            score = 2
        else:
            score = 1
        scored.append((pos, line, score))
    room = cap - len(head)
    chosen = []
    for want in (3, 2, 1):
        for pos, line, score in scored:
            if score == want and len(line) <= room:
                chosen.append((pos, score))
                room -= len(line)

    def build(keep):
        out, gap = [head], False
        for pos, line, score in scored:
            if pos in keep:
                if gap:
                    out.append(MARK)
                    gap = False
                out.append(line)
            elif score >= 0:
                gap = True
        if gap:
            out.append(MARK.rstrip())
        return "".join(out)

    keep = {p for p, _ in chosen}
    result = build(keep)
    for pos, score in sorted(chosen, key=lambda c: (c[1], -c[0])):   # markers made it too long: drop the least useful lines
        if len(result) <= cap:
            break
        keep.discard(pos)
        result = build(keep)
    result = result[:cap]
    dropped = sum(len(line) for pos, line, score in scored if score == 0)
    STATS["chars_boilerplate_dropped"] += dropped
    if not _YEARS.search(text[:cap]) and _YEARS.search(result):
        STATS["years_recovered"] += 1
    return result
