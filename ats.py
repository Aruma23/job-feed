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
import email.utils
import html as _html
import json
import random
import re
import threading
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


# ---- request manager: one place for limits, retries and backoff, shared by every board and discovery call ----
# Requests per second per provider. Concurrency alone doesn't cap the rate (a few fast workers can still exceed a limit), so
# every request waits for its provider's next free slot. Workday is limited per employer host, the rest per provider.
LIMITS = [("boards-api.greenhouse.io", 8), ("api.eu.lever.co", 2), ("api.lever.co", 2), ("api.ashbyhq.com", 4),
          ("api.smartrecruiters.com", 4), ("myworkdayjobs.com", 2), ("breezy.hr", 2), ("recruitee.com", 3), ("teamtailor.com", 2),
          ("workable.com", 1), ("pinpointhq.com", 2), ("rippling.com", 2), ("personio.de", 2), ("personio.com", 2),
          ("data.commoncrawl.org", 15), ("index.commoncrawl.org", 0.5)]
PER_HOST = ("myworkdayjobs.com",)
DEFAULT_RPS = 4
STATS = {}                 # provider -> {"requests", "retries", "throttled", "failed"} for feed_status.json
_LOCK = threading.Lock()
_NEXT = {}


def _provider(url):
    host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    for suffix, rps in LIMITS:
        if host == suffix or host.endswith("." + suffix):
            return (host if suffix in PER_HOST else suffix), rps
    return host, DEFAULT_RPS


def _wait_turn(key, rps):
    with _LOCK:
        now = time.time()
        t = max(now, _NEXT.get(key, 0.0))
        _NEXT[key] = t + 1.0 / rps
    if t > now:
        time.sleep(t - now)


def _hold(key, seconds):
    """A provider said slow down: every thread waits."""
    with _LOCK:
        _NEXT[key] = max(_NEXT.get(key, 0.0), time.time() + seconds)


def _retry_after(headers):
    v = (headers or {}).get("Retry-After") if headers else None
    if not v:
        return None
    try:
        return max(1.0, float(v))
    except ValueError:
        try:
            return max(1.0, email.utils.parsedate_to_datetime(v).timestamp() - time.time())
        except Exception:
            return None


def _backoff(i):
    return min(30.0, 2 * 3 ** i) * (0.75 + random.random() / 2)


def _count(key, field):
    with _LOCK:
        d = STATS.setdefault(key, {"requests": 0, "retries": 0, "throttled": 0, "failed": 0})
        d[field] += 1


def http(url, data=None, method=None, tries=3, timeout=TIMEOUT, want_json=True, opener=None, raw=False, headers=None):
    """GET/POST through the provider's rate limit. 429/503 honour Retry-After (capped at 60 s, and every thread backs off);
    other 5xx, timeouts and HTML pages where JSON was expected back off exponentially with jitter. 400/401/403/404/410 raise
    at once: the board doesn't exist or isn't public, which is different from a request that failed."""
    body = json.dumps(data).encode() if data is not None else None
    key, rps = _provider(url)
    last = None
    for i in range(tries):
        _wait_turn(key, rps)
        _count(key, "requests")
        req = urllib.request.Request(url, data=body, method=method or ("POST" if body else "GET"),
                                     headers=dict({"User-Agent": UA, "Accept": "application/json" if want_json else "*/*",
                                                   **({"Content-Type": "application/json"} if body else {})}, **(headers or {})))
        try:
            with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
                payload = r.read()
                if raw:
                    return payload
                if not want_json:
                    return payload.decode("utf-8", "replace")
                try:
                    return json.loads(payload.decode("utf-8", "replace"))
                except ValueError:
                    last = HttpError(-1, url)            # an HTML "slow down" page instead of JSON
                    wait = _backoff(i)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 400, 401, 403):
                raise HttpError(e.code, url)
            last = HttpError(e.code, url)
            if e.code in (429, 503):
                _count(key, "throttled")
                wait = min(60.0, _retry_after(e.headers) or _backoff(i + 1))
                _hold(key, wait)
            else:
                wait = _backoff(i)
        except Exception as e:  # timeouts, resets
            last = e
            wait = _backoff(i)
        if i < tries - 1:
            _count(key, "retries")
            time.sleep(wait)
    _count(key, "failed")
    raise last


def content_length(url):
    """Size of a file, from a one-byte range request."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    key, rps = _provider(url)
    _wait_turn(key, rps)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        cr = r.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)$", cr)
        return int(m.group(1)) if m else int(r.headers.get("Content-Length") or 0)


class Jobs(list):
    """A board's jobs. complete=False when the read covers only part of the board (Workday searches, capped paging): a job
    missing from a partial read is not closed until its own page says so."""
    complete = True


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
    if m and m.group(1).lower() not in ("external-referrals", "oneclick-ui", "api", "sr-jobs", "static", "robots", "sitemap"):
        return "smartrecruiters:" + m.group(1)
    m = re.search(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?([A-Za-z0-9_-]+)", u)
    if m and m.group(3).lower() not in ("wday", "job", "robots", "login", "sitemap") and len(m.group(3)) > 2 and m.group(1) not in ("www", "api"):
        return f"workday:{m.group(1)}|{m.group(2)}|{m.group(3)}"
    m = re.search(r"apply\.workable\.com/([A-Za-z0-9_-]+)", u)
    if m and m.group(1).lower() not in ("api", "j", "careers", "static", "assets"):
        return "workable:" + m.group(1).lower()
    m = re.search(r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", u)
    if m and m.group(1).lower() not in ("api", "static", "assets", "_next"):
        return "rippling:" + m.group(1).lower()
    m = re.search(r"https?://([a-z0-9-]+)\.jobs\.personio\.(de|com)\b", u, re.I)
    if m and m.group(1).lower() not in ("www", "api"):
        return "personio:" + m.group(1).lower()
    m = re.search(r"https?://([a-z0-9-]+)\.(breezy\.hr|recruitee\.com|teamtailor\.com|pinpointhq\.com)\b", u, re.I)
    if m and m.group(1).lower() not in SUBDOMAIN_RESERVED:
        kind = {"breezy.hr": "breezy", "recruitee.com": "recruitee", "teamtailor.com": "teamtailor", "pinpointhq.com": "pinpoint"}[m.group(2).lower()]
        return kind + ":" + m.group(1).lower()
    return ""


SUBDOMAIN_RESERVED = {"www", "app", "api", "help", "support", "blog", "status", "docs", "marketing", "career", "careers", "partner",
                      "partners", "developers", "developer", "cdn", "assets", "static", "mail", "go", "info", "jobs", "demo", "login",
                      "auth", "admin", "sandbox", "staging", "test", "academy", "community", "updates", "changelog", "security"}


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
    out, offset = Jobs(), 0
    out.complete = False
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
            out.complete = True
            break
        offset += 100
    return out


WORKDAY_SEARCHES = ["financial planning", "FP&A", "financial analyst", "strategic finance", "workforce planning", "business planning"]
WORKDAY_PAGES = 3          # 20 a page; a page that adds nothing new ends the search (Workday can repeat results across pages)


def workday(spec, want, get=http):
    tenant, shard, site = spec.split("|")
    base = f"https://{tenant}.{shard}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    seen, out = set(), Jobs()
    out.complete = False                      # searches, not the whole board: a missing job is checked on its own page
    for q in WORKDAY_SEARCHES:
        total = None
        for page in range(WORKDAY_PAGES):
            try:
                data = get(f"{api}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": 20 * page, "searchText": q})
            except HttpError as e:
                if e.code in (404, 400, 422) and not out and page == 0 and q == WORKDAY_SEARCHES[0]:
                    raise
                break
            if total is None:
                total = int(data.get("total") or 0)        # only the first page carries the real total
            fresh = 0
            for j in data.get("jobPostings") or []:
                path = j.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                fresh += 1
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
            if not fresh or 20 * (page + 1) >= (total or 0):
                break
    return out


# ---- more application systems (Nov 1 2026; formats checked live with probe.py) ----
def _loc(*parts):
    return ", ".join(p for p in (str(x or "").strip() for x in parts) if p)


def _flat_location(x):
    """A location object of unknown shape -> 'City, Region, Country' from whatever string fields it has."""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "; ".join(filter(None, (_flat_location(i) for i in x)))
    if isinstance(x, dict):
        for k in ("name", "label", "full_name", "location"):
            if isinstance(x.get(k), str) and x[k].strip():
                return x[k]
        return _loc(*(x.get(k) if isinstance(x.get(k), str) else (x.get(k) or {}).get("name", "") if isinstance(x.get(k), dict) else ""
                      for k in ("city", "addressLocality", "province", "state", "region", "addressRegion", "country", "addressCountry")))
    return ""


def workable(slug, want, get=http):
    data = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    out = Jobs()
    for j in data.get("jobs") or []:
        locs = j.get("locations") or [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}]
        loc = "; ".join(_loc(l.get("city"), l.get("region"), l.get("country")) for l in locs if not l.get("hidden"))
        out.append({"id": f"workable:{j.get('shortcode')}", "title": j.get("title", ""), "company": data.get("name") or slug,
                    "location": loc + ("; Remote" if j.get("telecommuting") else ""), "remote": bool(j.get("telecommuting")),
                    "url": j.get("url") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/", "posted": str(j.get("published_on") or j.get("created_at") or "")[:10],
                    "description": text_of(j.get("description"))})
    return out


def breezy(slug, want, get=http):
    data = get(f"https://{slug}.breezy.hr/json")
    out = Jobs()
    for j in data if isinstance(data, list) else []:
        l = j.get("location") or {}
        loc = _loc(l.get("city"), (l.get("state") or {}).get("id") if isinstance(l.get("state"), dict) else l.get("state"),
                   (l.get("country") or {}).get("name") if isinstance(l.get("country"), dict) else l.get("country")) or l.get("name", "")
        job = {"id": f"breezy:{j.get('id')}", "title": j.get("name", ""), "company": ((j.get("company") or {}).get("name")) or slug,
               "location": loc + ("; Remote" if l.get("is_remote") else ""), "remote": bool(l.get("is_remote")), "url": j.get("url", ""),
               "posted": str(j.get("published_date") or "")[:10], "description": ""}
        pay = re.findall(r"\$\s?(\d{2,3}(?:,\d{3})+)", str(j.get("salary") or ""))
        if len(pay) >= 2:
            job.update(pay_min=pay[0].replace(",", ""), pay_max=pay[1].replace(",", ""), interval="yearly")
        if want(job) and job["url"]:
            try:
                page = get(job["url"], want_json=False)
                m = re.search(r'(?is)<div class="description">(.*?)</div>\s*(?:<div class="apply|<footer|<div class="(?:job-)?footer)', page)
                job["description"] = text_of(m.group(1) if m else "")
            except Exception:
                pass
        out.append(job)
    return out


def recruitee(slug, want, get=http):
    data = get(f"https://{slug}.recruitee.com/api/offers/")
    out = Jobs()
    for j in data.get("offers") or []:
        locs = j.get("locations") or []
        loc = "; ".join(_loc(l.get("city"), l.get("state"), l.get("country")) for l in locs) or _loc(j.get("city"), j.get("state_name"), j.get("country"))
        sal = j.get("salary") or {}
        out.append({"id": f"recruitee:{j.get('id') or j.get('guid')}", "title": j.get("title", ""), "company": j.get("company_name") or slug,
                    "location": loc + ("; Remote" if j.get("remote") else ""), "remote": bool(j.get("remote")), "url": j.get("careers_url", ""),
                    "posted": str(j.get("published_at") or "")[:10], "description": text_of(j.get("description")) + "\n\n" + text_of(j.get("requirements")),
                    "pay_min": sal.get("min") or "", "pay_max": sal.get("max") or "",
                    "interval": "yearly" if "year" in str(sal.get("period") or "").lower() else (sal.get("period") or "")})
    return out


def teamtailor(slug, want, get=http):
    data = get(f"https://{slug}.teamtailor.com/jobs.json")
    out = Jobs()
    for j in data.get("items") or []:
        jp = j.get("_jobposting") or {}
        locs = jp.get("jobLocation") or []
        loc = _flat_location([(l.get("address") if isinstance(l, dict) else l) for l in (locs if isinstance(locs, list) else [locs])])
        remote = "TELECOMMUTE" in str(jp.get("jobLocationType") or "").upper()
        out.append({"id": f"teamtailor:{j.get('id')}", "title": j.get("title", ""), "company": (jp.get("hiringOrganization") or {}).get("name") or data.get("title") or slug,
                    "location": loc + ("; Remote" if remote else ""), "remote": remote, "url": j.get("url", ""), "posted": str(j.get("date_published") or "")[:10],
                    "description": text_of(j.get("content_html"))})
    return out


def pinpoint(slug, want, get=http):
    data = get(f"https://{slug}.pinpointhq.com/postings.json")
    out = Jobs()
    for j in data.get("data") or []:
        wt = str(j.get("workplace_type") or "").lower()
        freq = str(j.get("compensation_frequency") or "").lower()
        out.append({"id": f"pinpoint:{j.get('id')}", "title": j.get("title", ""), "company": slug, "location": _flat_location(j.get("location")) + ("; Remote" if wt == "remote" else ""),
                    "remote": wt == "remote", "url": j.get("url", ""), "posted": "",
                    "description": "\n\n".join(text_of(j.get(k)) for k in ("description", "key_responsibilities", "skills_knowledge_expertise")),
                    "pay_min": j.get("compensation_minimum") or "", "pay_max": j.get("compensation_maximum") or "",
                    "interval": {"year": "yearly", "hour": "hourly", "month": "monthly"}.get(freq, freq)})
    return out


def rippling(slug, want, get=http, pages=10):
    out, page = Jobs(), 0
    out.complete = False
    while page < pages:
        data = get(f"https://ats.rippling.com/api/v2/board/{slug}/jobs?page={page}&pageSize=50")
        for j in data.get("items") or []:
            locs = j.get("locations") or []
            remote = any(str(l.get("workplaceType") or "").upper() == "REMOTE" for l in locs)
            job = {"id": f"rippling:{j.get('id')}", "title": j.get("name", ""), "company": slug,
                   "location": "; ".join(_loc(l.get("city"), l.get("stateCode") or l.get("state"), l.get("country")) or l.get("name", "") for l in locs) + ("; Remote" if remote else ""),
                   "remote": remote, "url": j.get("url", ""), "posted": "", "description": ""}
            if want(job):
                try:
                    d = get(f"https://ats.rippling.com/api/v2/board/{slug}/jobs/{j.get('id')}")
                    job["description"] = text_of(d.get("description") or (d.get("descriptions") or {}).get("role") or d.get("content") or "")
                except Exception:
                    pass
            out.append(job)
        page += 1
        if page >= int(data.get("totalPages") or 0):
            out.complete = True
            break
    return out


def personio(slug, want, get=http):
    data = get(f"https://{slug}.jobs.personio.de/search.json")
    out = Jobs()
    for j in data if isinstance(data, list) else []:
        out.append({"id": f"personio:{j.get('id')}", "title": j.get("name", ""), "company": j.get("subcompany") or slug,
                    "location": "; ".join(j.get("offices") or [j.get("office") or ""]), "remote": "remote" in str(j.get("office") or "").lower(),
                    "url": f"https://{slug}.jobs.personio.de/job/{j.get('id')}", "posted": "", "description": str(j.get("description") or "")})
    return out


FETCH = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters, "workday": workday,
         "workable": workable, "breezy": breezy, "recruitee": recruitee, "teamtailor": teamtailor, "pinpoint": pinpoint,
         "rippling": rippling, "personio": personio}
KINDS = tuple(FETCH)


def fetch_board(board, want, get=http):
    kind, _, slug = board.partition(":")
    jobs = FETCH[kind](slug, want, get)
    if not isinstance(jobs, Jobs):
        jobs = Jobs(jobs)
    return jobs


def peek(board, get=http):
    """Light check of a board for the registry: its jobs without descriptions. Workday: one search near New York."""
    kind, _, slug = board.partition(":")
    if kind == "workday":
        tenant, shard, site = slug.split("|")
        data = get(f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                   data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "New York"}, tries=2)
        return Jobs({"title": j.get("title", ""), "location": j.get("locationsText", ""), "remote": "remote" in str(j.get("locationsText", "")).lower(),
                     "company": tenant} for j in data.get("jobPostings") or [])
    if kind == "rippling":
        return rippling(slug, lambda j: False, get, pages=3)
    return fetch_board(board, lambda j: False, get)


def still_open(job_url, get=http):
    """For jobs missing from a partial read: True (its page is up), False (gone), None (can't tell)."""
    u = str(job_url or "")
    m = re.match(r"(https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Za-z]{2}/)?([^/]+)(/job/.+)$", u)
    try:
        if m:
            get(f"{m.group(1)}/wday/cxs/{m.group(2)}/{m.group(4)}{m.group(5)}", tries=2)
            return True
        m = re.match(r"https://jobs\.smartrecruiters\.com/([^/]+)/(\d+)", u)
        if m:
            get(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}", tries=2)
            return True
        m = re.match(r"https://ats\.rippling\.com/([^/]+)/jobs/([0-9a-f-]+)", u)
        if m:
            get(f"https://ats.rippling.com/api/v2/board/{m.group(1)}/jobs/{m.group(2)}", tries=2)
            return True
    except HttpError as e:
        return False if e.code in (404, 410) else None
    except Exception:
        return None
    return None


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
