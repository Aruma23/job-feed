"""Direct employer sources for the job feed (v3, Nov 1 2026): the public job-board APIs of Greenhouse, Lever, Ashby,
SmartRecruiters and Workday, read the way their own career pages read them. No sign-in, no keys.

Gotchas handled (from running these against real boards, and from arabinowe/job-board-apis, clearfetch/ats-jobs-scraper,
repomesh/ashby-job-scraper and blakegrudzien/find-jobs):
- Greenhouse: `content` is HTML-escaped twice; unescape before stripping tags. Listing without content is cheap; details
  are fetched only for matching jobs.
- Lever: EU companies live on api.eu.lever.co; `createdAt` is milliseconds; the description is split over three fields.
- Ashby: `isRemote` is true for hybrid jobs too, so `workplaceType` decides; unlisted jobs are skipped; pay can come in
  several tiers (min of mins, max of maxes).
- SmartRecruiters: an unknown company returns 200 with zero jobs, not 404.
- Workday: needs tenant + shard + site; at most 20 a page; list dates are relative ("Posted 3 Days Ago"); cloud IPs get
  rate-limited with HTML pages, so few threads and backoff.
- Redirect links in aggregator rows (grnh.se short links, recruitics/appcast trackers) are opened to find the real board.
"""
import html as _html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; job-feed/3; +https://github.com/Aruma23/job-feed)"
TIMEOUT = 20


class HttpError(Exception):
    def __init__(self, code, url):
        super().__init__(f"HTTP {code} for {url[:120]}")
        self.code = code


def http(url, data=None, method=None, tries=3, timeout=TIMEOUT, want_json=True, opener=None):
    """GET/POST with retries on 429/5xx and on HTML error pages where JSON was expected. 404 raises at once."""
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"),
                                     headers={"User-Agent": UA, "Accept": "application/json" if want_json else "*/*",
                                              **({"Content-Type": "application/json"} if body else {})})
        try:
            with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
                raw = r.read()
                if not want_json:
                    return raw.decode("utf-8", "replace")
                try:
                    return json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    last = HttpError(-1, url)            # an HTML "slow down" page instead of JSON
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 400, 401, 403):
                raise HttpError(e.code, url)
            last = HttpError(e.code, url)
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(min(30, 2 * 3 ** i))
    raise last


def text_of(h):
    """HTML (possibly escaped twice) to plain text with line breaks kept."""
    s = _html.unescape(_html.unescape(str(h or "")))
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def ms_date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        return ""


def wd_posted(s, today=None):
    """Workday's 'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' to a date."""
    today = today or datetime.now(timezone.utc).date()
    s = str(s or "").lower()
    if "today" in s:
        return today.isoformat()
    if "yesterday" in s:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days?", s)
    return (today - timedelta(days=int(m.group(1)))).isoformat() if m else ""


def board_of(url):
    """'greenhouse:slug', 'lever:slug', 'ashby:slug', 'smartrecruiters:Company', 'workday:tenant|wd5|Site' or ''."""
    u = str(url or "")
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", u)
    if m and m.group(1) not in ("embed", "v1", "boards"):
        return "greenhouse:" + m.group(1).lower()
    m = re.search(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)", u)
    if m:
        return "lever:" + m.group(1).lower()
    m = re.search(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", u)
    if m and m.group(1).lower() not in ("api", "b", "meeting"):
        return "ashby:" + urllib.parse.unquote(m.group(1))
    m = re.search(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", u)
    if m:
        return "smartrecruiters:" + m.group(1)
    m = re.search(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", u)
    if m and m.group(3).lower() not in ("wday", "job"):
        return f"workday:{m.group(1)}|{m.group(2)}|{m.group(3)}"
    return ""


REDIRECTS = re.compile(r"^https?://(grnh\.se|click\.appcast\.io|jsv3\.recruitics\.com|[a-z0-9.-]*jobs?\.?redirect)", re.I)


def unwrap(url, resolve=None):
    """A tracking or short link to the real posting: recruitics carries it in rx_url; grnh.se and appcast redirect."""
    u = str(url or "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    for k in ("rx_url", "url", "dest", "redirect"):
        if q.get(k) and q[k][0].startswith("http"):
            return q[k][0]
    if REDIRECTS.match(u) and resolve:
        try:
            return resolve(u) or u
        except Exception:
            return u
    return u


def resolve_redirect(u):
    """Follows redirects (HEAD, then GET) and returns where the link lands."""
    req = urllib.request.Request(u, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (405, 403):
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=TIMEOUT) as r:
                return r.geturl()
        raise


# ---------------- one board -> jobs ----------------
# Each job: id (the same key the tracker's finder uses), title, company, location, remote, url, posted, pay_min, pay_max,
# interval, description (only for jobs that pass `want`, to keep requests and the CSV small).

def greenhouse(slug, want, get=http):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    meta = {}
    try:
        meta = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    except Exception:
        pass
    co = meta.get("name") or slug
    out = []
    for j in data.get("jobs") or []:
        loc = (j.get("location") or {}).get("name", "")
        job = {"id": f"greenhouse:{j['id']}", "title": j.get("title", ""), "company": co, "location": loc, "remote": "remote" in loc.lower(),
               "url": j.get("absolute_url", ""), "posted": str(j.get("first_published") or j.get("updated_at") or "")[:10], "description": ""}
        if want(job):
            try:
                d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}")
                job["description"] = text_of(d.get("content"))
                pay = re.findall(r"\$\s?(\d{2,3}(?:,\d{3})+)", job["description"])
                if len(pay) >= 2:
                    job["pay_min"], job["pay_max"], job["interval"] = pay[0].replace(",", ""), pay[1].replace(",", ""), "yearly"
            except Exception:
                pass
        out.append(job)
    return out


def lever(slug, want, get=http):
    try:
        data = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    except HttpError as e:
        if e.code != 404:
            raise
        data = get(f"https://api.eu.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location") or ""]
        wt = str(j.get("workplaceType") or "").lower()
        desc = "\n\n".join([j.get("descriptionPlain") or ""] + [f"{x.get('text', '')}\n{text_of(x.get('content'))}" for x in j.get("lists") or []] + [j.get("additionalPlain") or ""])
        sr = j.get("salaryRange") or {}
        out.append({"id": f"lever:{j.get('id')}", "title": j.get("text", ""), "company": slug, "location": "; ".join(filter(None, locs)),
                    "remote": wt == "remote", "url": j.get("hostedUrl", ""), "posted": ms_date(j.get("createdAt")), "description": desc.strip(),
                    "pay_min": sr.get("min") or "", "pay_max": sr.get("max") or "", "interval": "yearly" if "year" in str(sr.get("interval", "")).lower() else (sr.get("interval") or "")})
    return out


def ashby(slug, want, get=http):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(slug)}?includeCompensation=true")
    out = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
        wt = str(j.get("workplaceType") or "")
        lo = hi = None
        for t in ((j.get("compensation") or {}).get("compensationTiers") or []):
            for c in t.get("components") or []:
                if str(c.get("compensationType", "")).lower() == "salary":
                    lo = c.get("minValue") if lo is None else min(lo, c.get("minValue") or lo)
                    hi = c.get("maxValue") if hi is None else max(hi, c.get("maxValue") or hi)
        out.append({"id": f"ashby:{j.get('id')}", "title": j.get("title", ""), "company": data.get("name") or slug, "location": "; ".join(filter(None, locs)) + ("; Remote" if wt == "Remote" else ""),
                    "remote": wt == "Remote", "url": j.get("jobUrl", ""), "posted": str(j.get("publishedAt") or "")[:10], "description": j.get("descriptionPlain") or "",
                    "pay_min": lo or "", "pay_max": hi or "", "interval": "yearly" if lo else ""})
    return out


def smartrecruiters(company, want, get=http):
    out, offset = [], 0
    while offset < 300:
        data = get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset={offset}")
        items = data.get("content") or []
        for j in items:
            loc = j.get("location") or {}
            job = {"id": f"smartrecruiters:{j.get('id')}", "title": j.get("name", ""), "company": (j.get("company") or {}).get("name") or company,
                   "location": ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])), "remote": bool(loc.get("remote")),
                   "url": f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}", "posted": str(j.get("releasedDate") or "")[:10], "description": ""}
            if want(job):
                try:
                    d = get(j.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{j.get('id')}")
                    secs = ((d.get("jobAd") or {}).get("sections") or {})
                    job["description"] = "\n\n".join(text_of((secs.get(k) or {}).get("text")) for k in ("jobDescription", "qualifications", "additionalInformation"))
                except Exception:
                    pass
            out.append(job)
        if len(items) < 100 or offset + 100 >= int(data.get("totalFound") or 0):
            break
        offset += 100
    return out


WORKDAY_SEARCHES = ["financial planning", "FP&A", "financial analyst", "strategic finance", "workforce planning", "business planning"]


def workday(spec, want, get=http):
    tenant, shard, site = spec.split("|")
    base = f"https://{tenant}.{shard}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    seen, out = set(), []
    for q in WORKDAY_SEARCHES:
        try:
            data = get(f"{api}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q})
        except HttpError as e:
            if e.code in (404, 400, 422):
                raise
            continue
        for j in data.get("jobPostings") or []:
            path = j.get("externalPath")
            if not path or path in seen:
                continue
            seen.add(path)
            job = {"id": f"workday:{path.split('_')[-1]}", "title": j.get("title", ""), "company": tenant, "location": j.get("locationsText", ""),
                   "remote": "remote" in str(j.get("locationsText", "")).lower(), "url": f"{base}/{site}{path}", "posted": wd_posted(j.get("postedOn")), "description": ""}
            if want(job):
                try:
                    d = get(f"{api}{path}").get("jobPostingInfo") or {}
                    job["description"] = text_of(d.get("jobDescription"))
                    if d.get("startDate"):
                        job["posted"] = str(d["startDate"])[:10]
                    if d.get("location"):
                        job["location"] = "; ".join([d["location"]] + (d.get("additionalLocations") or []))
                except Exception:
                    pass
            out.append(job)
        time.sleep(0.5)
    return out


FETCH = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters, "workday": workday}


def fetch_board(board, want, get=http):
    kind, _, slug = board.partition(":")
    return FETCH[kind](slug, want, get)


def probe(name, get=http):
    """Guesses a company's board from its name on Greenhouse, Lever, Ashby and SmartRecruiters. '' if none."""
    words = re.sub(r"[^a-z0-9 ]", "", str(name or "").lower().replace("&", " and ")).split()
    words = [w for w in words if w not in ("inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "group", "holdings")]
    if not words:
        return ""
    slugs = list(dict.fromkeys([ "".join(words), "-".join(words)] + (["".join(words[:-1])] if len(words) > 1 and words[-1] in ("health", "labs", "technologies", "financial", "capital", "media", "partners", "ai") else [])))
    for s in slugs:
        for kind, url, ok in (("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", lambda d: isinstance(d, dict) and d.get("jobs") is not None),
                              ("lever", f"https://api.lever.co/v0/postings/{s}?mode=json&limit=1", lambda d: isinstance(d, list) and len(d) > 0),
                              ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{s}", lambda d: isinstance(d, dict) and len(d.get("jobs") or []) > 0)):
            try:
                if ok(get(url, tries=1)):
                    return f"{kind}:{s}"
            except Exception:
                pass
    # SmartRecruiters company IDs are usually the name in CamelCase ("BoschGroup"); an unknown one answers 200 with no jobs
    for s in dict.fromkeys(["".join(w.capitalize() for w in words), "".join(words)]):
        try:
            d = get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1", tries=1)
            if isinstance(d, dict) and int(d.get("totalFound") or 0) > 0:
                return f"smartrecruiters:{s}"
        except Exception:
            pass
    return ""
