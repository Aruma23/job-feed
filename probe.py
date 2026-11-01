"""Live checks run on GitHub Actions (the sandbox where the feed is written can't reach these sites).
python probe.py cc|ats|all — prints what Common Crawl's index files and each new application system actually return."""
import json
import sys
import time

import ats
import ccindex

PREFIXES = {"workable": "com,workable,apply)/", "breezy": "hr,breezy,", "recruitee": "com,recruitee,", "teamtailor": "com,teamtailor,",
            "pinpoint": "com,pinpointhq,", "rippling": "com,rippling,ats)/", "personio": "de,personio,jobs,",
            "greenhouse": "io,greenhouse,boards)/", "lever": "co,lever,jobs)/", "ashby": "com,ashbyhq,jobs)/", "workday": "com,myworkdayjobs,"}
ENDPOINTS = {"workable": ["https://apply.workable.com/api/v1/widget/accounts/{t}?details=true"],
             "breezy": ["https://{t}.breezy.hr/json"],
             "recruitee": ["https://{t}.recruitee.com/api/offers/"],
             "teamtailor": ["https://{t}.teamtailor.com/jobs.json", "https://{t}.teamtailor.com/jobs.rss"],
             "pinpoint": ["https://{t}.pinpointhq.com/postings.json"],
             "rippling": ["https://api.rippling.com/platform/api/ats/v1/board/{t}/jobs", "https://ats.rippling.com/api/v2/board/{t}/jobs"],
             "personio": ["https://{t}.jobs.personio.de/search.json", "https://{t}.jobs.personio.de/xml"]}


def shape(x, depth=0):
    if isinstance(x, dict):
        return {k: shape(v, depth + 1) for k, v in list(x.items())[:40]} if depth < 3 else "{...}"
    if isinstance(x, list):
        return [shape(x[0], depth + 1), f"... {len(x)} items"] if x else []
    if isinstance(x, str):
        return x[:80]
    return x


def cc():
    t0 = time.time()
    cr = ccindex.crawls()
    print("crawls", len(cr), cr[:4])
    crawl = cr[0]
    url = ccindex.BASE.format(crawl=crawl) + "cluster.idx"
    total = ccindex.size_of(url)
    print("cluster.idx bytes", total)
    tokens = {}
    for kind, prefix in PREFIXES.items():
        t1 = time.time()
        lines, nxt = ccindex.find_blocks(crawl, prefix, max_lines=4000, size=total)
        keys = [l["key"] for l in lines]
        toks = sorted({ats.board_of(ccindex.surt_to_url(k)) for k in keys} - {""})
        need = sum(1 for a, b in zip(lines, lines[1:]) if ats.board_of(ccindex.surt_to_url(a["key"])) != ats.board_of(ccindex.surt_to_url(b["key"])))
        print(f"{kind}: {len(lines)} blocks, more={nxt is not None}, tokens from keys={len(toks)}, blocks to read={need}, {time.time() - t1:.1f}s",
              (keys[:1] + keys[-1:]) if keys else "")
        if lines:
            recs = ccindex.read_block(crawl, lines[min(1, len(lines) - 1)])
            btoks = sorted({ats.board_of(u) for _, u in recs} - {""})
            print(f"   one block: {len(recs)} records, {len(btoks)} boards, e.g. {btoks[:6]} {recs[0][1][:90] if recs else ''}")
            tokens[kind] = (btoks or toks)[:12]
    print("cc total", round(time.time() - t0, 1), "s; requests", json.dumps(ats.STATS))
    json.dump(tokens, open("probe_tokens.json", "w"))
    return tokens


def ats_check(tokens):
    for kind, eps in ENDPOINTS.items():
        found = 0
        for t in [x.split(":", 1)[1] for x in tokens.get(kind, [])]:
            for ep in eps:
                u = ep.format(t=t)
                try:
                    body = ats.http(u, want_json=not u.endswith(("xml", "rss")), tries=1)
                except Exception as e:
                    print(f"{kind} {u}: {type(e).__name__} {str(e)[:80]}")
                    continue
                if isinstance(body, str):
                    print(f"{kind} {u}: text {len(body)} chars: {body[:600]!r}")
                else:
                    print(f"{kind} {u}: JSON {json.dumps(shape(body))[:2500]}")
                found += 1
            if found >= 2:
                break


if __name__ == "__main__":
    what = (sys.argv[1:] or ["all"])[0]
    toks = cc() if what in ("cc", "all") else json.load(open("probe_tokens.json"))
    if what in ("ats", "all"):
        ats_check(toks)
