"""Reading Common Crawl's URL index straight from its files (Nov 1 2026), the way open-ats-feed does it.

Each crawl publishes cc-index/collections/<crawl>/indexes/cluster.idx (about 110 MB of text, one line per compressed block of
about 3,000 index records, sorted by SURT key) next to the blocks themselves (cdx-00000.gz ... cdx-00299.gz). Because
cluster.idx is sorted, one host prefix ("io,greenhouse,boards)/") is found by binary search with HTTP range requests (a few
hundred KB, not 110 MB), and only the blocks that can hold new board names are fetched, again by range.
These files are served from data.commoncrawl.org (CloudFront), not the rate-limited index.commoncrawl.org query API.

A block whose first key and the next block's first key name the same board holds nothing but that board, so it is skipped:
big boards with thousands of job pages cost nothing beyond the cluster.idx lines.
"""
import gzip
import json
import re
import urllib.request

import ats

BASE = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/"
CHUNK = 64 * 1024


def get_range(url, start, end):
    """Bytes start..end (inclusive) of a file, through the shared request manager (limits and retries)."""
    return ats.http(url, want_json=False, raw=True, headers={"Range": f"bytes={start}-{end}"})


def size_of(url):
    return ats.content_length(url)


def _key(line):
    return line.split(" ", 1)[0]


def _parse(line):
    # "io,greenhouse,boards)/acme/jobs/1 20250101000000\tcdx-00012.gz\t123456\t180000\t42"
    head, *rest = line.split("\t")
    if len(rest) < 3:
        return None
    return {"key": _key(head), "file": rest[0], "offset": int(rest[1]), "length": int(rest[2])}


def find_blocks(crawl, prefix, start_at=0, max_lines=200000, get=get_range, size=None):
    """cluster.idx lines for keys starting with `prefix` (plus the line before, whose block may hold the first ones).
    Returns (lines, next_offset or None when the prefix is finished)."""
    url = BASE.format(crawl=crawl) + "cluster.idx"
    total = size if size is not None else size_of(url)

    def first_line_after(pos):
        data = get(url, pos, min(total - 1, pos + 4095)).decode("utf-8", "replace")
        if pos > 0:
            nl = data.find("\n")
            if nl < 0:
                return None, None
            return data[nl + 1:].split("\n", 1)[0], pos + nl + 1
        return data.split("\n", 1)[0], 0

    if start_at:
        lo = start_at
    else:
        lo, hi = 0, total
        while hi - lo > 8192:                          # binary search on byte offsets
            mid = (lo + hi) // 2
            line, _ = first_line_after(mid)
            if line is None or _key(line) >= prefix:
                hi = mid
            else:
                lo = mid
    out, pos, prev = [], lo, None
    while pos < total and len(out) < max_lines:
        data = get(url, pos, min(total - 1, pos + CHUNK - 1)).decode("utf-8", "replace")
        if pos == lo and lo > 0 and not start_at:
            nl = data.find("\n")
            data, pos = data[nl + 1:], pos + nl + 1
        cut = data.rfind("\n")
        if cut < 0 and pos + len(data) < total:
            return out, pos
        body = data[:cut] if cut >= 0 else data
        for line in body.split("\n"):
            p = _parse(line)
            if not p:
                continue
            if p["key"] < prefix:
                prev = p
                continue
            if not p["key"].startswith(prefix):
                return ([prev] if prev and not out else []) + out, None
            if prev is not None and not out:
                out.append(prev)
            out.append(p)
        pos += len(body.encode("utf-8")) + 1
    return out, (pos if pos < total else None)


def read_block(crawl, block, get=get_range):
    """The index records in one block: [(surt key, original url)]."""
    raw = get(BASE.format(crawl=crawl) + block["file"], block["offset"], block["offset"] + block["length"] - 1)
    out = []
    for line in gzip.decompress(raw).decode("utf-8", "replace").splitlines():
        try:
            key, _, js = line.split(" ", 2)
            out.append((key, json.loads(js).get("url", "")))
        except ValueError:
            continue
    return out


def crawls(get_text=None):
    """Crawl ids, newest first."""
    get_text = get_text or (lambda u: ats.http(u, want_json=False, tries=2, timeout=60))
    return [c["id"] for c in json.loads(get_text("https://index.commoncrawl.org/collinfo.json"))]


def surt_to_url(key):
    """'io,greenhouse,boards)/acme/jobs/1' -> 'https://boards.greenhouse.io/acme/jobs/1' (lower case: SURT keys are)."""
    host, _, path = key.partition(")")
    parts = [p for p in host.split(",") if p]
    host = ".".join(reversed(parts)).split(":")[0]
    return "https://" + host + (path or "/")
