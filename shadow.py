"""Shadow adapters (feed v10, Nov 14 2026): new application systems tried inside the real run before they may add jobs.

The development sandbox can't reach employer sites, so an adapter's format is checked where the feed runs: each run calls the
candidate adapters for a few employers the coverage report names, records what came back in feed_status.json "shadow"
(HTTP status, whether the reply parsed, how many jobs, sample titles, whether a description could be read), and adds nothing
to jobs.csv. An adapter is switched on (moved into ats.FETCH) only after its shadow record shows it parsing real boards.

Candidates:
  oracle     Oracle Recruiting Cloud Candidate Experience: the public JSON API behind
             {host}/hcmUI/CandidateExperience/en/sites/{site}/... (JPMorgan Chase and other banks): recruitingCEJobRequisitions
             with finder=findReqs, and recruitingCEJobRequisitionDetails for one job's text.
  contacthr  {tenant}.contacthr.com/{n} links (JPMorgan Chase's 62 uncovered rows): where they land after redirects, so the
             real system behind them is learned.
Budget: SHADOW_HOSTS hosts a run, two requests each; every failure is recorded, never raised.
"""
import re
import urllib.parse

SHADOW_HOSTS = 6
ORACLE = re.compile(r"https://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([A-Za-z0-9_]+)/", re.I)
CONTACTHR = re.compile(r"https://[a-z0-9-]+\.contacthr\.com/\d+", re.I)


def oracle_list_url(host, site, keyword="finance", limit=25, offset=0):
    finder = (f"findReqs;siteNumber={site},facetsList=LOCATIONS;WORK_LOCATIONS;TITLES;CATEGORIES;POSTING_DATES,"
              f"limit={limit},offset={offset},keyword=\"{keyword}\",sortBy=POSTING_DATES_DESC")
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
            f"&expand=requisitionList.secondaryLocations&finder=" + urllib.parse.quote(finder, safe="=;,\""))


def oracle_detail_url(host, site, jid):
    finder = f"ById;Id=\"{jid}\",siteNumber={site}"
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder="
            + urllib.parse.quote(finder, safe="=;,\""))


def oracle_parse(data):
    """-> (total, [{id, title, location, posted}]) from a findReqs reply; raises if the shape is not the expected one."""
    item = (data.get("items") or [{}])[0]
    reqs = item.get("requisitionList")
    if reqs is None:
        raise ValueError("no requisitionList in the reply")
    jobs = [{"id": str(j.get("Id") or ""), "title": j.get("Title") or "", "location": j.get("PrimaryLocation") or "",
             "posted": str(j.get("PostedDate") or "")[:10]} for j in reqs]
    return int(item.get("TotalJobsCount") or len(jobs)), jobs


def oracle_text(data):
    d = (data.get("items") or [{}])[0]
    return " ".join(str(d.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))


def targets(rows):
    """Oracle (host, site) pairs and ContactHR links from the aggregator rows, most rows first."""
    ora, chr_ = {}, []
    for r in rows:
        u = str(r.get("job_url_direct") or "")
        m = ORACLE.match(u)
        if m:
            k = (m.group(1).lower(), m.group(2))
            ora[k] = ora.get(k, 0) + 1
        elif CONTACTHR.match(u) and len(chr_) < 3:
            chr_.append(u)
    return [k for k, _ in sorted(ora.items(), key=lambda kv: -kv[1])], chr_


def run(rows, get, resolve, budget=SHADOW_HOSTS):
    """Never raises. Returns the feed_status "shadow" section."""
    out = {"oracle": [], "contacthr": []}
    try:
        ora, chr_ = targets(rows)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    for host, site in ora[:budget]:
        rec = {"host": host, "site": site}
        try:
            total, jobs = oracle_parse(get(oracle_list_url(host, site)))
            rec.update(ok=True, total=total, got=len(jobs), sample=[j["title"][:60] for j in jobs[:3]],
                       posted=[j["posted"] for j in jobs[:3]])
            if jobs and jobs[0]["id"]:
                try:
                    text = oracle_text(get(oracle_detail_url(host, site, jobs[0]["id"])))
                    rec["detail_chars"] = len(text)
                except Exception as e:
                    rec["detail_error"] = f"{type(e).__name__}: {str(e)[:100]}"
        except Exception as e:
            rec.update(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
        out["oracle"].append(rec)
    for u in chr_:
        try:
            landed = resolve(u) or u
            out["contacthr"].append({"from": u, "landed": landed[:200], "oracle": bool(ORACLE.match(landed))})
        except Exception as e:
            out["contacthr"].append({"from": u, "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return out
