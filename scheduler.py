"""Adaptive search scheduler for the aggregator searches (Nov 1 2026).

search_stats.json keeps, per site and per search phrase, a 30-day daily log: runs, rows returned, unique rows, qualified rows
(right place + relevant by the rules) and qualified rows that were new to the feed. Each run:
  - a site whose searches all came back empty SITE_PAUSE_RUNS runs in a row is paused: one probe search a day until it
    returns rows again (Glassdoor, ZipRecruiter and Google Jobs often block cloud runners);
  - a search finishing its trial (TRIAL_RUNS runs) with no new qualified jobs over 7 days runs once a day ("reduced");
    none over 30 days (at least 20 runs) and it runs once a week ("paused");
  - freed slots go to trial searches from POOL (new phrases tried on their own merit), then to more results for the
    searches producing the most new qualified jobs.
The base list never shrinks below one search per working site, and a returning site gets its full list back.
"""
import json
import os
from datetime import date, timedelta

TRIAL_RUNS = 8             # about two days at four runs a day
SITE_PAUSE_RUNS = 6
REDUCED_EVERY = 4          # runs: about once a day
PAUSED_EVERY_DAYS = 7
MAX_SEARCHES = 28          # per run (time on the runner)
POOL_ADMIT = 2             # new trial searches added per run when there is room
BOOST_MAX = 100            # results asked for by a top search

# New phrases on trial. Kept to the same lanes (FP&A, strategic/corporate/commercial finance, workforce and capacity planning,
# adjacent finance); each must earn its place by bringing in new qualified jobs.
POOL = [
    # Nov 1 (v9): responsibility-based searches. Finance work advertised under other titles (Revenue Strategy, Business
    # Operations, Commercial Finance...). Indeed matches every word anywhere in the posting, so duty words find the work
    # whatever the title says; the relevance rules then judge each job by its duties, and each phrase still has to earn
    # its place with new qualified jobs. Listed first: untried phrases are admitted in this order, 2 a run
    # when there is room, so these go ahead of the older untried ones.
    ("indeed", "revenue strategy", 30), ("indeed", "commercial finance", 30), ("indeed", "finance and strategy manager", 30),
    ("indeed", "business planning manager", 30), ("indeed", "business operations forecasting budget", 30),
    ("indeed", "strategy operations forecast budget variance", 30), ("indeed", "pricing strategy financial model", 30),
    ("linkedin", "revenue strategy", 20), ("linkedin", "commercial finance", 20), ("linkedin", "strategy and operations finance", 20),
    ("indeed", "FP&A associate", 40), ("indeed", "senior financial analyst", 40), ("indeed", "commercial finance analyst", 30),
    ("indeed", "capacity planning analyst", 30), ("indeed", "workforce management analyst", 30), ("indeed", "budget analyst", 30),
    ("indeed", "revenue analyst", 30), ("indeed", "finance business partner", 30), ("indeed", "corporate development analyst", 30),
    ("indeed", "treasury analyst", 30), ("indeed", "forecasting analyst", 30), ("indeed", "operations finance analyst", 30),
    ("indeed", "strategy and operations analyst", 30), ("indeed", "planning and analysis associate", 30),
    ("linkedin", "financial planning and analysis", 25), ("linkedin", "corporate finance analyst", 20),
    ("linkedin", "capacity planning analyst", 20), ("linkedin", "business finance analyst", 20), ("linkedin", "senior financial analyst", 20),
    ("glassdoor", "financial planning and analysis", 30), ("zip_recruiter", "strategic finance", 30),
    ("google", "financial analyst jobs in New York, NY", 30),
]


def key(site, term):
    return f"{site}|{term}"


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict) and isinstance(s.get("searches"), dict):
            s.setdefault("sites", {})
            s.setdefault("run", 0)
            return s
    except (FileNotFoundError, ValueError):
        pass
    return {"v": 1, "run": 0, "searches": {}, "sites": {}}


def save(state, path):
    with open(path + ".new", "w", encoding="utf-8") as f:
        json.dump(state, f, indent=0, sort_keys=True)
    os.replace(path + ".new", path)


def _window(entry, today, days):
    d0 = date.fromisoformat(today) - timedelta(days=days - 1)
    tot = [0, 0, 0, 0, 0, 0]
    for d, v in (entry.get("days") or {}).items():
        if d >= d0.isoformat():
            tot = [a + b for a, b in zip(tot, v)]
    return dict(zip(("runs", "returned", "unique", "qualified", "qnew", "errors"), tot))


def _state_of(entry, today):
    if entry.get("runs_total", 0) < TRIAL_RUNS:
        return "trial"
    w7, w30 = _window(entry, today, 7), _window(entry, today, 30)
    if w30["runs"] >= 20 and w30["qnew"] == 0:
        return "paused"
    if w7["runs"] >= TRIAL_RUNS and w7["qnew"] == 0:
        return "reduced"
    return "active"


def plan(state, base, pool, today):
    """-> (searches to run [(site, term, n)], decisions {key: reason}). Advances the run counter."""
    state["run"] = run = state.get("run", 0) + 1
    entries = state["searches"]
    for s, t, n in base:
        entries.setdefault(key(s, t), {"site": s, "term": t, "n": n, "src": "base", "added": today, "runs_total": 0, "days": {}})
    decisions, chosen, freed = {}, [], 0
    sites = {s for s, _, _ in base} | {e["site"] for e in entries.values() if e.get("src") == "pool"}
    paused_sites = {s for s in sites if state["sites"].get(s, {}).get("state") == "paused"}
    probed = set()
    def pool_rank(k):
        w = _window(entries[k], today, 7)
        return (entries[k].get("state", "trial") != "trial", -w["qnew"] / max(1, w["runs"]))
    order = [key(s, t) for s, t, _ in base] + sorted((k for k, e in entries.items() if e.get("src") == "pool"), key=pool_rank)
    for k in order:
        e = entries[k]
        site = e["site"]
        if site in paused_sites:
            info = state["sites"][site]
            if site not in probed and info.get("probed") != today:
                probed.add(site)
                info["probed"] = today
                chosen.append(k)
                decisions[k] = "site paused: daily probe"
            else:
                decisions[k] = "site paused"
                freed += 1
            continue
        st = _state_of(e, today)
        e["state"] = st
        if st == "reduced" and (run + sum(map(ord, k))) % REDUCED_EVERY:
            decisions[k] = "reduced: no new qualified jobs in 7 days"
            freed += 1
            continue
        if st == "paused":
            last = e.get("last_run_day") or ""
            if last and (date.fromisoformat(today) - date.fromisoformat(last)).days < PAUSED_EVERY_DAYS:
                decisions[k] = "paused: no new qualified jobs in 30 days"
                freed += 1
                continue
        chosen.append(k)
        decisions[k] = st
    # trials from the pool when there is room
    room = MAX_SEARCHES - len(chosen)
    admitted = 0
    for s, t, n in pool:
        k = key(s, t)
        if k in entries or room <= 0 or admitted >= POOL_ADMIT or s in paused_sites:
            continue
        entries[k] = {"site": s, "term": t, "n": n, "src": "pool", "added": today, "runs_total": 0, "days": {}, "state": "trial"}
        chosen.append(k)
        decisions[k] = "trial (new phrase)"
        room -= 1
        admitted += 1
    chosen = chosen[:MAX_SEARCHES]
    # boost: the searches bringing the most new qualified jobs ask for more results when slots were freed
    sizes = {k: entries[k]["n"] for k in chosen}
    if freed:
        ranked = sorted((k for k in chosen if entries[k].get("state") == "active"),
                        key=lambda k: -_window(entries[k], today, 7)["qnew"] / max(1, _window(entries[k], today, 7)["runs"]))
        for k in ranked[:min(freed, 4)]:
            if _window(entries[k], today, 7)["qnew"] > 0:
                sizes[k] = min(BOOST_MAX, int(entries[k]["n"] * 1.5))
                decisions[k] = decisions[k] + f" (boosted to {sizes[k]})"
    return [(entries[k]["site"], entries[k]["term"], sizes[k]) for k in chosen], decisions


def record(state, status, today):
    """status: collect()'s per-search entries, with 'qualified' and 'qnew' filled in. Updates logs and site pauses."""
    by_site = {}
    for s in status:
        k = key(s["site"], s["search"])
        e = state["searches"].setdefault(k, {"site": s["site"], "term": s["search"], "n": 30, "src": "base", "added": today, "runs_total": 0, "days": {}})
        v = e["days"].setdefault(today, [0, 0, 0, 0, 0, 0])
        add = [1, s.get("returned", 0), s.get("new", 0), s.get("qualified", 0), s.get("qnew", 0), 1 if s.get("error") else 0]
        e["days"][today] = [a + b for a, b in zip(v, add)]
        e["runs_total"] = e.get("runs_total", 0) + 1
        e["last_run_day"] = today
        by_site[s["site"]] = by_site.get(s["site"], 0) + s.get("returned", 0)
    cutoff = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    for e in state["searches"].values():
        e["days"] = {d: v for d, v in e["days"].items() if d >= cutoff}
    for site, got in by_site.items():
        info = state["sites"].setdefault(site, {"state": "active", "zero_runs": 0})
        if got:
            if info.get("state") == "paused":
                info["resumed"] = today
            info.update(state="active", zero_runs=0)
        else:
            info["zero_runs"] = info.get("zero_runs", 0) + 1
            if info["zero_runs"] >= SITE_PAUSE_RUNS and info.get("state") != "paused":
                info.update(state="paused", since=today)


def report(state, today):
    """A compact view for feed_status.json: per site and per search, 7- and 30-day qualified-new counts and state."""
    out = {"run": state.get("run", 0), "sites": state.get("sites", {}), "searches": []}
    for k, e in sorted(state["searches"].items()):
        w7, w30 = _window(e, today, 7), _window(e, today, 30)
        out["searches"].append({"search": k, "state": e.get("state", ""), "src": e.get("src", ""), "runs_7d": w7["runs"],
                                "qualified_new_7d": w7["qnew"], "qualified_new_30d": w30["qnew"], "returned_7d": w7["returned"],
                                "qualified_per_run_7d": round(w7["qualified"] / w7["runs"], 1) if w7["runs"] else 0})
    return out
