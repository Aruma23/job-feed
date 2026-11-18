"""Change impact on the real feed (Nov 14 2026): what a code change does to the jobs already published, before it ships.

Why: unit tests prove a rule does what its author meant on made-up rows. They don't show what it does to the 3,000 real jobs the
tracker reads. On Nov 14 an outside audit's PR replaced the "many copies of one title" rule; its own tests passed, but on the
published feed it moved Clark Davis Associates (a recruiting firm, 47 rows) from intermediary to direct employer and lifted 12
of its ads into A/B, and fixed no misread employer. That only showed when both versions ran on the real jobs.csv. This script
does that for every pull request (checks.yml) and on demand.

Usage: python -I impact.py <base_tree> [<head_tree>] [<jobs.csv>]
  base_tree: a checkout of the code before the change (checks.yml makes one with git worktree)
  head_tree: the changed code (default: the directory this file is in)
  jobs.csv:  the published feed to run both on (default: head_tree/jobs.csv)
Prints a Markdown report (checks.yml appends it to the run's summary). Exit code 0 always: it informs, it doesn't block.

What it re-runs, per row, with each version's own modules: the relevance rules' fit for rows the rules scored (Nov 18; a model
verdict stands, it needs the network), employer typing (employer.annotate), requirements (reqs.requirements:
years, education, seniority), pay read from the text when the pay fields are empty (reqs.pay_from_text) and the priority tier
(priority.assess). It doesn't re-run searches, board reads, the model review or freshness (those need the network or a run's
state): their changes need their own tests. Public data only; nothing is written.
"""
import collections
import copy
import csv
import importlib
import os
import sys

MODULES = ("employer", "reqs", "priority", "relevance", "discover")
OPTIONAL = ("relevance", "discover")        # older trees are still compared on the rest
OWN = ("employer", "identity", "intel", "reqs", "priority", "freshness", "trust", "geo", "relevance", "textfit", "discover", "ats",
       "footprint", "harvest", "ccindex", "backlog", "history")


def _load(tree):
    for m in [m for m in list(sys.modules) if m in OWN]:
        del sys.modules[m]
    sys.path.insert(0, os.path.abspath(tree))
    out = {}
    for n in MODULES:
        try:
            out[n] = importlib.import_module(n)
        except Exception as e:                   # a tree without one of the modules (older code): report, don't crash
            if n in OPTIONAL:
                continue
            sys.path.pop(0)
            raise RuntimeError(f"{tree}: {type(e).__name__}: {e}")
    return out


def _refit(M, r, text):
    """Nov 18: the rules' fit for a row the rules scored (fit_by = rules). A model verdict needs the network, so it stands;
    but the rules decide every row the model never saw, which is most of the feed."""
    if "relevance" not in M or "discover" not in M:
        return
    ev = getattr(M["relevance"], "evidence_of", None)
    if r.get("fit_by", "rules") != "rules":
        if ev:                                         # the model verdict graded as this tree would (with the posting, if it can)
            v = {"by": "model", "why": r.get("fit_why"), "ev": r.get("fit_evidence") or None}
            try:
                r["fit_evidence"] = ev(v, text)
            except TypeError:
                r["fit_evidence"] = ev(v)
        return
    v = M["relevance"].rules(r.get("title"), text, M["discover"].title_kind(r.get("title")))
    r["fit_score"], r["fit_category"], r["fit_why"] = str(v["score"]), v["category"], v["why"]
    if ev:
        r["fit_evidence"] = ev(v)


def _same(base, head, names):
    """True when every named module file is byte-identical in both trees (then re-running it can't change anything)."""
    def read(t, n):
        try:
            with open(os.path.join(t, n + ".py"), "rb") as f:
                return f.read()
        except OSError:
            return None
    return all(read(base, n) == read(head, n) for n in names)


def run(tree, rows, requirements=True):
    """Annotated copies of rows under the code in tree. requirements=False keeps the published years/education/seniority
    (used when reqs.py is the same in both trees: re-parsing 3,000 postings twice would only show no change)."""
    M = _load(tree)
    try:
        out = copy.deepcopy(rows)
        try:
            M["employer"].annotate(out)
        except Exception as e:
            for r in out:
                r["employer_type"], r["employer_type_why"] = "error", f"{type(e).__name__}"
        for r in out:
            text = str(r.get("description") or "").replace("\\n", "\n")
            try:
                _refit(M, r, text)
            except Exception as e:
                r["fit_score"] = "error:" + type(e).__name__
            if requirements:
                try:
                    req = M["reqs"].requirements(r.get("title") or "", text)
                    r.update({k: req.get(k, r.get(k, "")) for k in ("years_min", "education", "seniority")})
                except Exception:
                    pass
            if not (r.get("min_amount") or r.get("max_amount")) and hasattr(M["reqs"], "pay_from_text"):
                try:
                    lo, hi = M["reqs"].pay_from_text(text)
                    r["pay_low"], r["pay_high"] = lo or r.get("pay_low", ""), hi or r.get("pay_high", "")
                except Exception:
                    pass
            try:
                M["priority"].assess(r)
            except Exception as e:
                r["priority"] = "error:" + type(e).__name__
        return out
    finally:
        sys.path.pop(0)


def report(base_rows, head_rows, limit=15):
    C = collections.Counter
    lines = ["## Change impact on the published feed", "",
             f"{len(head_rows)} published jobs re-scored with the code before and after this change.", ""]

    def moves(col):
        return C((b.get(col, ""), h.get(col, "")) for b, h in zip(base_rows, head_rows) if b.get(col, "") != h.get(col, ""))

    tiers = moves("priority")
    up = sum(n for (b, h), n in tiers.items() if h in ("A", "B") and b not in ("A", "B"))
    down = sum(n for (b, h), n in tiers.items() if b in ("A", "B") and h not in ("A", "B"))
    lines.append(f"**Priority tier: {sum(tiers.values())} jobs move; {up} newly A/B, {down} leave A/B.**")
    for (b, h), n in tiers.most_common(8):
        lines.append(f"- {b or '(none)'} → {h or '(none)'}: {n}")
    shown = [(b, h) for b, h in zip(base_rows, head_rows) if b.get("priority") != h.get("priority")]
    shown.sort(key=lambda x: (x[1].get("priority") not in ("A", "B"), x[1].get("company", "")))
    if shown:
        lines += ["", "| job | tier | employer type | why (after) |", "|---|---|---|---|"]
        for b, h in shown[:limit]:
            why = str(h.get("priority_why", "")).replace("|", "/")[:110]
            lines.append(f"| {h.get('company', '')[:28]}: {h.get('title', '')[:48]} | {b.get('priority')} → {h.get('priority')} | "
                         f"{b.get('employer_type', '')} → {h.get('employer_type', '')} | {why} |")
    fit = [(b, h) for b, h in zip(base_rows, head_rows) if b.get("fit_score") != h.get("fit_score")]
    if fit:
        def n(x):
            try:
                return int(x.get("fit_score") or 0)
            except ValueError:
                return -1
        cross_up = sum(1 for b, h in fit if n(b) < 50 <= n(h))
        cross_down = sum(1 for b, h in fit if n(h) < 50 <= n(b))
        lines += ["", f"**Fit score (rules): {len(fit)} jobs change; {cross_up} newly reach 50 (qualified), {cross_down} drop below.**"]
        for b, h in sorted(fit, key=lambda x: -abs(n(x[1]) - n(x[0])))[:6]:
            lines.append(f"- {h.get('company', '')[:28]}: {h.get('title', '')[:48]}: {b.get('fit_score')} → {h.get('fit_score')}")
    cat = moves("fit_category")
    if cat:
        lines += ["", f"**Fit category: {sum(cat.values())} jobs change.**"]
        for (b, h), k in cat.most_common(6):
            lines.append(f"- {b[:40] or '(empty)'} → {h[:40] or '(empty)'}: {k}")
    for col, label in (("employer_type", "Employer type"), ("years_min", "Years asked"), ("seniority", "Seniority"),
                       ("education", "Education"), ("pay_high", "Pay read from the text (top of range)")):
        m = moves(col)
        if not m:
            continue
        lines += ["", f"**{label}: {sum(m.values())} jobs change.**"]
        for (b, h), n in m.most_common(6):
            lines.append(f"- {str(b)[:40] or '(empty)'} → {str(h)[:40] or '(empty)'}: {n}")
    if not any(moves(c) for c in ("priority", "fit_score", "fit_category", "employer_type", "years_min", "seniority", "education", "pay_high")):
        lines += ["", "No published job changes under the re-run parts. (Search, board-read, review and freshness changes "
                  "need their own tests.)"]
    return "\n".join(lines) + "\n"


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    here = os.path.dirname(os.path.abspath(__file__))
    base = argv[1]
    head = argv[2] if len(argv) > 2 else here
    path = argv[3] if len(argv) > 3 else os.path.join(head, "jobs.csv")
    csv.field_size_limit(10 ** 9)
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    req = not _same(base, head, ("reqs", "textfit"))
    try:
        b, h = run(base, rows, req), run(head, rows, req)
    except RuntimeError as e:
        print(f"## Change impact on the published feed\n\nCouldn't compare: {e}\n")
        return 0
    print(report(b, h))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
