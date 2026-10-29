#!/usr/bin/env python3
"""Deterministic gating, scoring, ranking and robustness analysis.
No network, no randomness, no LLM.

Design rules this file enforces
  * score = f(verified facts, criteria version). Nothing here reads another proposal while
    scoring one; ranking is a final sort. Adding a bid cannot reorder the others.
  * A proposal is excluded (ineligible) only on affirmative evidence of failure. Missing,
    disputed or ungrounded facts, integrity flags and limit breaches give `flagged`:
    held out of the ranking until a human resolves them. Fail closed, never guess.
  * Integrity flags can never raise or lower a score. They only route to a human.
  * Nothing is dropped silently: every uploaded id comes out exactly once.

Usage
  python score.py                                  # score fixtures in extractions/
  python score.py --extractions extractions_local  # score the output of extract.py
  python score.py --stress                         # robustness of the ordering
  python score.py --lint                           # lint + preview the criteria configuration
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORDER = {"eligible": 0, "flagged": 1, "ineligible": 2}


# ----------------------------------------------------------------------------- helpers
def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical_hash(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def fact(ex: dict, field_id: str):
    """A value counts only if the pipeline marked it `found` and it survived verification."""
    f = ex["fields"].get(field_id)
    if not f or f.get("status") != "found":
        return None
    return f.get("value")


def linear(value, zero_at, full_at):
    """Sub-score 0-100: `zero_at` earns 0, `full_at` earns 100, linear between, clamped."""
    if value is None:
        return None
    t = (value - zero_at) / (full_at - zero_at)
    return max(0.0, min(1.0, t)) * 100.0


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ----------------------------------------------------------------------------- criteria lint
def lint_criteria(c: dict) -> tuple[list[str], list[str]]:
    """Admin-screen validation. Errors block publishing; warnings are shown beside the preview."""
    errors, warnings = [], []
    field_ids = {f["id"] for f in c.get("fields", [])}
    weighted_ids = [w["id"] for w in c["weighted"]]

    def need(fid, where):
        if fid not in field_ids:
            errors.append(f"{where}: unknown field '{fid}'")

    total = sum(w["weight"] for w in c["weighted"])
    if abs(total - 100) > 1e-9:
        errors.append(f"weights must total 100, got {total:g}")
    if len(set(weighted_ids)) != len(weighted_ids):
        errors.append("duplicate criterion ids")
    for m in c["mandatory"]:
        for r in m["rules"]:
            need(r["field"], f"mandatory '{m['id']}'")
    for lim in c.get("limits", []):
        need(lim["field"], "limit")
    limits = {lim["field"]: lim for lim in c.get("limits", [])}
    for w in c["weighted"]:
        where = f"criterion '{w['id']}'"
        if w.get("on_missing", "flag") not in ("flag", "ineligible"):
            errors.append(f"{where}: on_missing must be 'flag' or 'ineligible'")
        if w["type"] in ("linear", "composite"):
            parts = [w] if w["type"] == "linear" else w["parts"]
            if w["type"] == "composite" and abs(sum(p["share"] for p in parts) - 1) > 1e-9:
                errors.append(f"{where}: composite shares must total 1")
            for p in parts:
                need(p["field"], where)
                if p["zero_at"] == p["full_at"]:
                    errors.append(f"{where}: zero_at and full_at are equal for '{p['field']}'")
                lim = limits.get(p["field"])
                if lim and p["full_at"] < p["zero_at"] and p["zero_at"] > lim["max"]:
                    warnings.append(f"{where}: zero_at is beyond the {lim['label']}; bids over the limit "
                                    f"would still earn points (they are flagged regardless)")
        elif w["type"] == "checklist":
            for i in w["items"]:
                need(i, where)
        else:
            errors.append(f"{where}: unknown type '{w['type']}'")
    for t in c.get("tie_breakers", []):
        if t["by"] == "subscore" and t["id"] not in weighted_ids:
            errors.append(f"tie-breaker: unknown criterion '{t['id']}'")
        if t["by"] == "field":
            need(t["id"], "tie-breaker")
    return errors, warnings


def validate_criteria(c: dict) -> None:
    errors, _ = lint_criteria(c)
    if errors:
        raise ValueError("; ".join(errors))


def preview(c: dict) -> list[dict]:
    """What the admin sees before publishing: a probe bid at each anchor and halfway."""
    rows = []
    for w in c["weighted"]:
        if w["type"] == "checklist":
            continue
        for p in ([w] if w["type"] == "linear" else w["parts"]):
            mid = (p["zero_at"] + p["full_at"]) / 2
            rows.append({"criterion": w["id"], "field": p["field"],
                         "zero_at": p["zero_at"], "midpoint": mid, "full_at": p["full_at"],
                         "sub_scores": [round(linear(v, p["zero_at"], p["full_at"]), 1)
                                        for v in (p["zero_at"], mid, p["full_at"])]})
    return rows


# ----------------------------------------------------------------------------- gates
def check_rule(rule: dict, ex: dict) -> str:
    v = fact(ex, rule["field"])
    if v is None:
        return "unknown"
    if rule["op"] == "is_true":
        ok = v is True
    elif rule["op"] == "gte":
        ok = v >= rule["value"]
    else:
        raise ValueError(f"unknown op {rule['op']}")
    return "pass" if ok else "fail"


def eval_mandatory(c: dict, ex: dict) -> list[dict]:
    out = []
    for m in c["mandatory"]:
        rs = [check_rule(r, ex) for r in m["rules"]]
        result = "fail" if "fail" in rs else ("unknown" if "unknown" in rs else "pass")
        out.append({"id": m["id"], "label": m["label"], "result": result})
    return out


# ----------------------------------------------------------------------------- scoring
def score_criterion(crit: dict, ex: dict) -> dict:
    """Return sub-score bounds. lo == hi unless a needed fact is missing (then 0..100)."""
    missing, detail = [], {}
    if crit["type"] == "linear":
        v = fact(ex, crit["field"])
        detail[crit["field"]] = v
        sub = linear(v, crit["zero_at"], crit["full_at"])
        if sub is None:
            missing.append(crit["field"])
            lo, hi = 0.0, 100.0
        else:
            lo = hi = sub
    elif crit["type"] == "composite":
        lo = hi = 0.0
        for p in crit["parts"]:
            v = fact(ex, p["field"])
            detail[p["field"]] = v
            sub = linear(v, p["zero_at"], p["full_at"])
            if sub is None:
                missing.append(p["field"])
                hi += p["share"] * 100.0
            else:
                lo += p["share"] * sub
                hi += p["share"] * sub
    elif crit["type"] == "checklist":
        met = [i for i in crit["items"] if fact(ex, i) is True]
        detail = {"met": met, "not_met": [i for i in crit["items"] if i not in met]}
        lo = hi = 100.0 * len(met) / len(crit["items"])  # absence = no credit, never a flag
    else:
        raise ValueError(f"unknown criterion type {crit['type']}")
    return {"id": crit["id"], "label": crit["label"], "weight": crit["weight"],
            "sub_lo": lo, "sub_hi": hi, "missing": missing, "detail": detail}


def experience_window_note(c: dict, ex: dict):
    """Soft note: is the stated experience period inside the 5-year window?"""
    window_start_year = int(c["evaluation_date"][:4]) - 5
    back = fact(ex, "installs_period_years_back")
    start = fact(ex, "installs_period_start_year")
    if back is not None:
        return None if back <= 5 else f"experience period is {back:g} years, longer than the 5-year window"
    if start is not None:
        if start < window_start_year:
            return (f"experience counted 'since {start}': includes installs older than the 5-year "
                    f"window, so the count is an upper bound")
        if start == window_start_year:
            return f"experience counted 'since {start}': may include installs just outside the 5-year window"
    return None


def evaluate(c: dict, ex: dict, decisions: dict | None = None) -> dict:
    """Score one proposal. `decisions` carries recorded human decisions (waivers, cleared flags)."""
    dec = decisions or {}
    waived, cleared = set(dec.get("waive_mandatory", [])), set(dec.get("cleared_flags", []))
    mandatory = eval_mandatory(c, ex)
    notes = []
    for m in mandatory:
        if m["id"] in waived and m["result"] != "pass":
            m["result"], m["waived"] = "pass", True
            notes.append(f"mandatory criterion '{m['id']}' waived by recorded human decision")
    crits = [score_criterion(w, ex) for w in c["weighted"]]
    lo = sum(x["weight"] / 100 * x["sub_lo"] for x in crits)
    hi = sum(x["weight"] / 100 * x["sub_hi"] for x in crits)

    flags = []
    for m in mandatory:
        if m["result"] == "unknown":
            flags.append({"type": "mandatory_unverified", "detail": f"cannot verify: {m['label']}"})
    for x, w in zip(crits, c["weighted"]):
        for fid in x["missing"]:
            fr = ex["fields"].get(fid) or {}
            st, note = fr.get("status", "absent"), fr.get("note")
            why = f" ({note})" if note else ""
            if w.get("on_missing", "flag") == "ineligible" and st in ("not_stated", "ambiguous"):
                mandatory.append({"id": f"responsive:{w['id']}", "result": "fail",
                                  "label": f"must state {w['label'].lower()} information ({fid} {st.replace('_', ' ')})"})
            else:
                flags.append({"type": "missing_info",
                              "detail": f"{fid} {st.replace('_', ' ')}{why}; '{x['id']}' cannot be scored"})
    for lim in c["limits"]:
        v = fact(ex, lim["field"])
        if v is not None and v > lim["max"]:
            flags.append({"type": "limit_breach", "detail": f"{lim['field']}={v:g} exceeds {lim['label']}"})
    for f in ex.get("integrity_flags", []):
        if f["type"] in cleared:
            notes.append(f"integrity flag '{f['type']}' cleared by recorded human decision")
        else:
            flags.append({"type": "integrity" if f["type"] != "pipeline_failure" else "pipeline_failure",
                          "detail": f"{f['type']} on p.{f.get('page', '?')}: {f.get('note', 'see quote')}"})
    n = experience_window_note(c, ex)
    if n:
        notes.append(n)

    if any(m["result"] == "fail" for m in mandatory):
        status = "ineligible"
    elif flags:
        status = "flagged"
    else:
        status = "eligible"

    return {
        "proposal": ex["proposal_id"], "status": status, "mandatory": mandatory,
        "criteria": [{**x, "points_lo": round(x["weight"] / 100 * x["sub_lo"], 2),
                      "points_hi": round(x["weight"] / 100 * x["sub_hi"], 2)} for x in crits],
        "score_lo": round(lo, 1), "score_hi": round(hi, 1),
        "flags": flags, "notes": notes,
        "_sub": {x["id"]: x["sub_lo"] for x in crits},
        "_fields": {k: fact(ex, k) for k in ex["fields"]},
    }


# ----------------------------------------------------------------------------- ranking
def sort_key(c: dict, r: dict):
    key = [-r["score_lo"]]
    for t in c["tie_breakers"]:
        v = r["_sub"][t["id"]] if t["by"] == "subscore" else r["_fields"].get(t["id"])
        v = float("inf") if v is None else v
        key.append(-v if (t["prefer"] == "higher" and v != float("inf")) else v)
    return tuple(key)


def rank_all(c: dict, results: list[dict]) -> list[dict]:
    eligible = sorted((r for r in results if r["status"] == "eligible"), key=lambda r: sort_key(c, r))
    prev_key, prev_rank = None, 0
    for i, r in enumerate(eligible, 1):
        k = sort_key(c, r)
        r["rank"] = prev_rank if k == prev_key else i
        prev_key, prev_rank = k, r["rank"]
    for r in results:
        r.setdefault("rank", None)
    return results


def add_envelopes(results: list[dict]) -> None:
    """For each unranked bid: the places it could take if it alone were resolved.
    Tells a reviewer exactly how much each flag matters. Ties are counted pessimistically."""
    ranked = [r["score_lo"] for r in results if r["status"] == "eligible"]
    for r in results:
        if r["status"] == "eligible":
            r["envelope"] = None
            continue
        best = 1 + sum(s > r["score_hi"] for s in ranked)
        worst = 1 + sum(s >= r["score_lo"] for s in ranked)
        r["envelope"] = {"best": best, "worst": worst}


def envelope_text(env: dict) -> str:
    if env["best"] == env["worst"]:
        return f"would rank {ordinal(env['best'])}"
    return f"could rank {ordinal(env['best'])} to {ordinal(env['worst'])}"


def reason(r: dict) -> str:
    if r["status"] == "ineligible":
        fails = "; ".join(m["label"] for m in r["mandatory"] if m["result"] == "fail")
        return (f"Ineligible (proposed): fails mandatory criterion ({fails}). Reviewers must confirm. "
                f"If overturned alone it {envelope_text(r['envelope'])} (indicative score {r['score_lo']}).")
    if r["status"] == "flagged":
        what = "; ".join(f["detail"] for f in r["flags"])
        rng = (f"indicative score {r['score_lo']}" if r["score_lo"] == r["score_hi"]
               else f"provisional score {r['score_lo']} to {r['score_hi']}")
        return f"Flagged, held out of ranking: {what}. If cleared alone it {envelope_text(r['envelope'])} ({rng})."
    top = max(r["criteria"], key=lambda x: x["points_lo"] / x["weight"])
    low = min(r["criteria"], key=lambda x: x["points_lo"] / x["weight"])
    return (f"Passes both mandatory checks; {r['score_lo']} pts. Strongest: {top['label'].lower()}; "
            f"weakest: {low['label'].lower()}.")


# ----------------------------------------------------------------------------- robustness
def stub_extraction(pid: str) -> dict:
    """Fail closed: an uploaded file with no verified extraction is flagged, never dropped."""
    return {"proposal_id": pid, "fields": {},
            "integrity_flags": [{"type": "pipeline_failure", "page": None,
                                 "note": "no verified extraction available (failed or pending)"}]}


def evaluate_all(c, exs, decisions):
    return [evaluate(c, ex, decisions.get(ex["proposal_id"])) for ex in exs]


def variants(c: dict):
    """Plausible alternative configurations: scale each anchor distance, shift weights."""
    cfg = c.get("robustness", {})
    scales, shift = cfg.get("anchor_scales", [0.8, 1.0, 1.25]), cfg.get("weight_shift", 5)
    anchors = []
    for i, w in enumerate(c["weighted"]):
        if w["type"] == "linear":
            anchors.append((i, None))
        elif w["type"] == "composite":
            anchors += [(i, j) for j in range(len(w["parts"]))]
    for combo in itertools.product(scales, repeat=len(anchors)):
        v = copy.deepcopy(c)
        for (i, j), s in zip(anchors, combo):
            tgt = v["weighted"][i] if j is None else v["weighted"][i]["parts"][j]
            tgt["full_at"] = tgt["zero_at"] + (tgt["full_at"] - tgt["zero_at"]) * s
        yield v
    for a, b in itertools.permutations(range(len(c["weighted"])), 2):
        if c["weighted"][a]["weight"] >= shift:
            v = copy.deepcopy(c)
            v["weighted"][a]["weight"] -= shift
            v["weighted"][b]["weight"] += shift
            yield v


def robustness(c: dict, exs: list[dict], decisions: dict) -> dict | None:
    """Is the winner an artefact of my anchors and weights? Which unresolved bids could decide it?"""
    base = {r["proposal"]: r for r in evaluate_all(c, exs, decisions)}
    eligible = [p for p, r in base.items() if r["status"] == "eligible"]
    if not eligible:
        return None
    winner = min((base[p] for p in eligible), key=lambda r: sort_key(c, r))["proposal"]
    unresolved = [p for p, r in base.items() if r["status"] != "eligible"]
    n = stable = 0
    could = {u: 0 for u in unresolved}
    for v in variants(c):
        res = {r["proposal"]: r for r in evaluate_all(v, exs, decisions)}
        scores = {p: res[p]["score_lo"] for p in eligible}
        top_id = max(sorted(scores), key=lambda p: scores[p])
        n += 1
        stable += top_id == winner
        for u in unresolved:
            if res[u]["score_hi"] > scores[top_id]:
                could[u] += 1
    return {"variants": n, "winner": winner,
            "winner_stable_pct": round(100 * stable / n, 1),
            "unresolved_could_win_pct": {u: round(100 * k / n, 1) for u, k in could.items()}}


# ----------------------------------------------------------------------------- run
def run(c: dict, extractions: list[dict], decisions: dict | None = None,
        uploaded_ids: list[str] | None = None, with_robustness: bool = True) -> dict:
    validate_criteria(c)
    decisions = decisions or {}
    exs = list(extractions)
    ids = [e["proposal_id"] for e in exs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate proposal ids in extractions")
    for pid in (uploaded_ids or []):
        if pid not in ids:
            exs.append(stub_extraction(pid))
    results = rank_all(c, evaluate_all(c, exs, decisions))
    add_envelopes(results)
    results.sort(key=lambda r: (ORDER[r["status"]], r["rank"] or 0, r["proposal"]))

    ranked = [r for r in results if r["status"] == "eligible"]
    margin = round(ranked[0]["score_lo"] - ranked[1]["score_lo"], 1) if len(ranked) >= 2 else None
    close_call = margin is not None and margin < c["governance"]["close_call_margin"]
    rob = robustness(c, exs, decisions) if with_robustness else None
    critical = sorted(
        {r["proposal"] for r in results if r["envelope"] and r["envelope"]["best"] == 1}
        | {u for u, pct in (rob or {}).get("unresolved_could_win_pct", {}).items() if pct > 0})

    ranking = [{
        "proposal": r["proposal"], "rank": r["rank"],
        "status": "ranked" if r["status"] == "eligible" else r["status"],
        "score": r["score_lo"] if r["status"] == "eligible" else None,
        "reason": reason(r),
    } for r in results]
    public = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    return {
        "run": {"criteria_version": c["criteria_version"], "criteria_sha256": canonical_hash(c),
                "evaluation_date": c["evaluation_date"], "proposals_in": len(exs),
                "proposals_out": len(results)},
        "governance": {"provisional": any(r["status"] != "eligible" for r in results),
                       "margin_to_second": margin, "close_call": close_call,
                       "rank_critical_unresolved": critical, "robustness": rob},
        "ranking": ranking, "breakdown": public,
    }


def compare_runs(old: dict, new: dict) -> list[dict]:
    """Q4: what changed when a criteria version changed. Shown to the reviewer before accepting."""
    o = {r["proposal"]: r for r in old["ranking"]}
    n = {r["proposal"]: r for r in new["ranking"]}
    return [{"proposal": p, "old": (o[p]["status"], o[p]["rank"], o[p]["score"]),
             "new": (n[p]["status"], n[p]["rank"], n[p]["score"])} for p in sorted(o)]


# ----------------------------------------------------------------------------- cli
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", default=str(HERE / "criteria.json"))
    ap.add_argument("--extractions", default=str(HERE / "extractions"))
    ap.add_argument("--out", default=str(HERE / "output"))
    ap.add_argument("--uploaded", help="comma-separated ids that were uploaded; any without an extraction "
                                       "file is flagged as a pipeline failure instead of being dropped")
    ap.add_argument("--stress", action="store_true")
    ap.add_argument("--lint", action="store_true")
    args = ap.parse_args()

    c = load_json(args.criteria)
    if args.lint:
        errors, warnings = lint_criteria(c)
        print("errors  :", errors or "none")
        print("warnings:", warnings or "none")
        for row in preview(c):
            print(f"  {row['criterion']:<11} {row['field']:<28} {row['zero_at']:>9g} -> 0 | "
                  f"{row['midpoint']:>9g} -> 50 | {row['full_at']:>9g} -> 100")
        return

    exs = [load_json(p) for p in sorted(Path(args.extractions).glob("*.json"))]
    result = run(c, exs, uploaded_ids=args.uploaded.split(",") if args.uploaded else None)
    rob = result["governance"]["robustness"]
    if args.stress:
        print(f"configurations tested : {rob['variants']}")
        print(f"current winner        : {rob['winner']}  (first in {rob['winner_stable_pct']}% of them)")
        for u, pct in rob["unresolved_could_win_pct"].items():
            print(f"  unresolved {u}: best case beats the winner in {pct}% of configurations")
        return

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    (out / "ranking.json").write_text(json.dumps(result["ranking"], indent=2) + "\n", encoding="utf-8")
    (out / "breakdown.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    g = result["governance"]
    print(f"criteria {result['run']['criteria_version']}  sha256 {result['run']['criteria_sha256'][:12]}…  "
          f"in={result['run']['proposals_in']} out={result['run']['proposals_out']}")
    print(f"ranking is {'PROVISIONAL (unresolved bids)' if g['provisional'] else 'complete'}; "
          f"margin to second: {g['margin_to_second']}; close call: {g['close_call']}; "
          f"could decide the winner: {g['rank_critical_unresolved'] or 'none'}\n")
    print(f"{'ID':<3} {'status':<11} {'rank':<5} {'score':<12} {'if resolved alone':<20} "
          + " ".join(f"{x['id'][:6]:>7}" for x in c["weighted"]))
    for b in result["breakdown"]:
        sc = f"{b['score_lo']}" if b["score_lo"] == b["score_hi"] else f"{b['score_lo']}-{b['score_hi']}"
        env = envelope_text(b["envelope"]) if b["envelope"] else ""
        pts = " ".join(f"{x['points_lo']:>7.2f}" for x in b["criteria"])
        print(f"{b['proposal']:<3} {b['status']:<11} {str(b['rank'] or '-'):<5} {sc:<12} {env:<20} {pts}")
    print("\n(points per criterion at their lower bound; unranked rows are indicative only)")


if __name__ == "__main__":
    main()
