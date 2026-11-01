"""Which rows are the same opening (Nov 1 2026). Replaces "same company + same title = same job".

Strongest evidence first:
  1. a requisition or posting ID: the application system's own job ID read from the link (Greenhouse, Lever, Ashby,
     SmartRecruiters, Workday, iCIMS, Taleo, SuccessFactors, Oracle, Jobvite, Workable, BambooHR, Dayforce, UKG, Paylocity,
     Rippling, ADP), the site's posting ID (Indeed jk, LinkedIn job ID), or a "Req ID: R12345" line in the description;
  2. the employer's application link with tracking parameters removed;
  3. company + normalized title + location (all of NYC counts as one place, elsewhere the town).
Rows merge only when they are the same opening: a fallback match is refused when both rows carry different requisition IDs,
different employer job links, or are two different postings on the same site.
"""
import re
import urllib.parse

import geo

AGGREGATOR = re.compile(r"(^|\.)(indeed|linkedin|glassdoor|ziprecruiter|google)\.", re.I)
# Query parameters that only track where a click came from. Everything else is kept: on tracker and ATS links the parameters
# often ARE the job (jometer jz=, recruitics rx_jobId=, ADP r=), so dropping them would make different jobs look alike.
TRACKING = re.compile(r"^(utm_.*|gh_src|lever-(source|origin|via)|source|src|sourcetype|jobsource|ref|refid|referrer|trk|trackingid|"
                      r"iis|iisn|bid|clickid|click_id|campaign|medium|mode|in_iframe|fbclid|gclid|msclkid|mc_[ce]id|_ga|_gl|hs[a-z_]*|vjs|tk|"
                      r"from|step|applicationstep|feedid|jobpipeline|rx_(ch|source|medium|campaign|group|ts|viewer|p|c|a|paid|job_feed)|"
                      r"sid|codes|ss|spa|li_fat_id|_hs.*)$", re.I)
_ATS = [
    ("gh", re.compile(r"greenhouse\.io/(?:[^/?#]+/)?jobs/(\d+)", re.I)),
    ("gh", re.compile(r"[?&]gh_jid=(\d+)", re.I)),
    ("lever", re.compile(r"lever\.co/[^/?#]+/([0-9a-f]{8}-[0-9a-f-]{27,})", re.I)),
    ("ashby", re.compile(r"ashbyhq\.com/[^/?#]+/([0-9a-f]{8}-[0-9a-f-]{27,})", re.I)),
    ("sr", re.compile(r"smartrecruiters\.com/[^/?#]+/(\d{6,})", re.I)),
    ("wd", re.compile(r"//([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com/[^?#]*?_([A-Za-z0-9-]+)(?:/apply[^?#]*)?(?:[?#]|$)", re.I)),
    ("icims", re.compile(r"//([a-z0-9-]+)\.icims\.com/jobs/(\d+)", re.I)),
    ("taleo", re.compile(r"//([a-z0-9-]+)\.taleo\.net/.*?[?&]job=([A-Za-z0-9-]+)", re.I)),
    ("sf", re.compile(r"[?&]career_job_req_id=(\d+)", re.I)),
    ("oracle", re.compile(r"//([a-z0-9-]+)\.[a-z0-9.-]*oraclecloud\.com/.*?/(?:job|requisitions/preview)/(\d+)", re.I)),
    ("jobvite", re.compile(r"jobvite\.com/[^/?#]+/job/([A-Za-z0-9]+)", re.I)),
    ("workable", re.compile(r"workable\.com/[^/?#]+/j/([A-Za-z0-9]+)", re.I)),
    ("bamboo", re.compile(r"//([a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs/view\.php\?id=)/?(\d+)", re.I)),
    ("dayforce", re.compile(r"dayforcehcm\.com/[^?#]*/jobs/(\d+)", re.I)),
    ("ukg", re.compile(r"ultipro\.com/.*?opportunityid=([0-9a-f-]{36})", re.I)),
    ("paylocity", re.compile(r"paylocity\.com/recruiting/jobs/details/(\d+)", re.I)),
    ("rippling", re.compile(r"rippling\.com/[^/?#]+/jobs/([0-9a-f-]{36})", re.I)),
    ("adp", re.compile(r"adp\.com/.*?[?&]jobid=([A-Za-z0-9_.-]+)", re.I)),
]
_POSTING = [("indeed", re.compile(r"indeed\.com/.*?[?&]v?jk=([0-9a-f]{8,})", re.I)),
            ("li", re.compile(r"linkedin\.com/(?:jobs/view/(?:[^/?#]*-)?|.*?[?&]currentjobid=)(\d{6,})", re.I))]
_DESC_REQ = re.compile(r"\b(?:requisition|req|job|position|posting)\s*(?:id|#|number|no\.?|code)\s*[:#.\-]?\s*([A-Z]{0,4}[-_]?\d{3,}[A-Z0-9-]*)\b", re.I)
_NATIVE = {"greenhouse": "gh", "lever": "lever", "ashby": "ashby", "smartrecruiters": "sr"}

SUFFIX = re.compile(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|companies|plc|lp|llp|na|group|holdings|usa|us|the|"
                    r"international|global)\b")


def emp_key(name):
    s = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower().replace("&", " and ").replace("'", ""))
    return re.sub(r"\s+", " ", SUFFIX.sub(" ", s).replace(" and ", " ")).strip()


_MODE = r"(?:hybrid|remote|on ?site|in ?office|in person|flexible|full ?time|permanent|contract|temporary)"


def title_ident(t):
    """A title for identity: spelling variants and work-mode or location tags removed, but real distinctions kept
    ("Financial Analyst (Treasury)" and "Financial Analyst (FP&A)" stay different)."""
    s = str(t or "").lower().replace("&", " and ")
    s = re.sub(r"\(([^)]*)\)", lambda m: " " if re.fullmatch(r"[\s,/-]*(?:" + _MODE + r"[\s,/-]*)+", m.group(1)) or
               geo.resolve(m.group(1)).status in ("local", "remote_us") else " " + m.group(1) + " ", s)
    m = re.search(r"\s[-–—|,:]\s([^-–—|]+)$", s)
    if m and (re.fullmatch(r"\s*(?:" + _MODE + r"\s*)+", m.group(1)) or geo.resolve(m.group(1)).status in ("local", "remote_us")):
        s = s[:m.start()]
    s = re.sub(r"\bfp\s*and\s*a\b|\bfpa\b|financial planning and analysis", " fpa ", s)
    s = re.sub(r"\bsr\b\.?", " senior ", s)
    s = re.sub(r"\bjr\b\.?", " junior ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def canon_url(u):
    """The link with tracking removed: host without www, path without /apply and trailing slash, only identifying params."""
    u = str(u or "").strip()
    if not u.startswith("http"):
        return ""
    p = urllib.parse.urlsplit(u)
    host = p.netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    path = re.sub(r"/(apply|application|apply-now|login|applynow)/?$", "", p.path.rstrip("/"), flags=re.I).rstrip("/")
    q = sorted((k.lower(), v) for k, v in urllib.parse.parse_qsl(p.query) if not TRACKING.match(k))
    return host + path.lower() + ("?" + urllib.parse.urlencode(q) if q else "")


def ats_keys(url):
    """Requisition and posting keys a link carries: {'gh:123'}, {'wd:tenant:R12345'}, {'indeed:abc123'}."""
    out = set()
    u = str(url or "")
    if not u:
        return out
    for fam, rx in _ATS + _POSTING:
        m = rx.search(u)
        if m:
            out.add(fam + ":" + ":".join(g.lower() for g in m.groups()))
    return out


def req_keys(row):
    """Requisition keys (the opening itself), separate from posting keys (one site's listing of it)."""
    keys = set()
    for u in (row.get("job_url_direct"), row.get("job_url")):
        keys |= {k for k in ats_keys(u) if not k.startswith(("indeed:", "li:"))}
    rid = str(row.get("id") or "")
    kind, _, num = rid.partition(":")
    if kind in _NATIVE and num:
        keys.add(_NATIVE[kind] + ":" + num.lower())
    m = _DESC_REQ.search(str(row.get("description") or "")[:6000])
    if m and re.search(r"\d", m.group(1)):
        keys.add("desc:" + emp_key(row.get("company")) + ":" + m.group(1).upper())
    return keys


def posting_keys(row):
    keys = {"id:" + i for i in source_ids(row)}
    for u in (row.get("job_url"), row.get("job_url_direct")):
        keys |= {k for k in ats_keys(u) if k.startswith(("indeed:", "li:"))}
    return keys


def url_keys(row):
    out = set()
    for u in (row.get("job_url_direct"), row.get("job_url")):
        c = canon_url(u)
        # only links that point at one job (an ID in them); a bare careers page is shared by every opening
        if c and not AGGREGATOR.search(c.split("/")[0]) and re.search(r"\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}", c.split("/", 1)[-1]):
            out.add("url:" + c)
    return out


def fallback_key(row):
    return "ctl:" + emp_key(row.get("company")) + "|" + title_ident(row.get("title")) + "|" + geo.loc_key(row.get("location"), str(row.get("is_remote")) == "True")


def source_ids(row):
    ids = [x for x in str(row.get("source_ids") or "").split(";") if x]
    if row.get("id") and row["id"] not in ids:
        ids.insert(0, row["id"])
    return ids


def _prefix(i):
    return re.split(r"[-:]", i, maxsplit=1)[0]


def req_label(keys):
    """The requisition ID to show: the employer's own (Workday R-number, Greenhouse job ID ...) before one read from text."""
    ranked = sorted(keys, key=lambda k: (k.startswith("desc:"), k))
    return ranked[0] if ranked else ""


class Index:
    """Rows already in the feed, findable by any of their keys."""

    def __init__(self):
        self.rows, self.by = [], {}

    def add(self, row):
        slot = len(self.rows)
        self.rows.append(row)
        self._index(slot, row)
        return slot

    def _index(self, slot, row):
        for k in posting_keys(row) | req_keys(row) | url_keys(row) | {fallback_key(row)}:
            self.by.setdefault(k, [])
            if slot not in self.by[k]:
                self.by[k].append(slot)

    def update(self, slot, row):
        self.rows[slot] = row
        self._index(slot, row)

    def find(self, row):
        """Slot of the same opening, or None."""
        rq, ps, us = req_keys(row), posting_keys(row), url_keys(row)
        for k in sorted(ps) + sorted(rq) + sorted(us):
            for slot in self.by.get(k, []):
                if not self._conflict(self.rows[slot], row, rq, strong=True):
                    return slot
        for slot in self.by.get(fallback_key(row), []):
            if not self._conflict(self.rows[slot], row, rq, strong=False):
                return slot
        return None

    @staticmethod
    def _conflict(old, new, new_req, strong):
        old_req = req_keys(old)
        fams = {k.split(":")[0] for k in old_req} & {k.split(":")[0] for k in new_req}
        for f in fams:                                   # both carry an ID from the same system and the IDs differ
            if not ({k for k in old_req if k.startswith(f + ":")} & {k for k in new_req if k.startswith(f + ":")}):
                return True
        if strong:
            return False
        if old_req and new_req and not (old_req & new_req):
            return True
        ou = {k for k in url_keys(old) if ats_keys("https://" + k[4:])}
        nu = {k for k in url_keys(new) if ats_keys("https://" + k[4:])}
        if ou and nu and not (ou & nu):
            return True                                  # two different employer job links
        olds, nid = source_ids(old), str(new.get("id") or "")
        if nid and nid not in olds and any(_prefix(o) == _prefix(nid) for o in olds):
            return True                                  # two different postings on the same site
        return False
