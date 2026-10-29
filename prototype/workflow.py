#!/usr/bin/env python3
"""Workflow layer: proposal states, human actions, finalisation gate, tamper-evident audit log.

This module never computes a score. It records human decisions and asks score.py to recompute.
Guarantees enforced here (and covered by test_workflow.py)
  1. Nothing silently dropped: every uploaded id has exactly one visible state; in == out.
  2. Fail closed: a missing extraction is NEEDS_REVIEW, never ranked and never excluded.
  3. Exclusion is a human act: the engine can only *propose* it; two different people confirm.
  4. Clarification may fill a gap. It can never overwrite a stated value or change price.
  5. The award cannot be recorded while any bid is unresolved, evidence is unopened, a close
     call is unacknowledged, or the approver took part in the decisions.
  6. The audit log is hash-chained, so any edit to history is detectable.

  python workflow.py        # runs an end-to-end demo on the sample set, prints the transcript
"""
from __future__ import annotations

import copy
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path

import score

HERE = Path(__file__).resolve().parent


class GateError(Exception):
    """A control refused an action. The refusal is itself written to the audit log."""


# ----------------------------------------------------------------------------- audit log
class AuditLog:
    GENESIS = "0" * 64

    def __init__(self, clock=None):
        self.entries: list[dict] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def append(self, actor: str, action: str, payload: dict | None = None) -> dict:
        body = {"seq": len(self.entries) + 1, "ts": self._clock(), "actor": actor, "action": action,
                "payload": payload or {}, "prev": self.entries[-1]["hash"] if self.entries else self.GENESIS}
        body["hash"] = score.canonical_hash(body)
        self.entries.append(body)
        return body

    def verify(self) -> tuple[bool, int | None]:
        prev = self.GENESIS
        for e in self.entries:
            core = {k: v for k, v in e.items() if k != "hash"}
            if e["prev"] != prev or score.canonical_hash(core) != e["hash"]:
                return False, e["seq"]
            prev = e["hash"]
        return True, None

    def export(self, path) -> None:
        Path(path).write_text("\n".join(json.dumps(e, sort_keys=True) for e in self.entries) + "\n",
                              encoding="utf-8")


# ----------------------------------------------------------------------------- run
class Run:
    def __init__(self, criteria: dict, uploaded_ids: list[str], extractions: list[dict],
                 log: AuditLog | None = None, started_by: str = "system"):
        score.validate_criteria(criteria)
        self.criteria = criteria
        self.uploaded = list(uploaded_ids)
        self.extractions = {e["proposal_id"]: copy.deepcopy(e) for e in extractions}
        self.decisions = {pid: {"waive_mandatory": [], "cleared_flags": []} for pid in self.uploaded}
        self.dispositions: dict[str, dict] = {}
        self.opened: dict[str, set] = {}
        self.maker_actors: set[str] = set()
        self.close_call_ack: str | None = None
        self.finalised: dict | None = None
        self.log = log or AuditLog()
        self.log.append(started_by, "run_started", {
            "criteria_version": criteria["criteria_version"], "criteria_sha256": score.canonical_hash(criteria),
            "uploaded": self.uploaded, "evaluation_date": criteria["evaluation_date"]})
        self._recompute()

    # ---- derived state --------------------------------------------------------
    def _recompute(self) -> None:
        self.result = score.run(self.criteria, list(self.extractions.values()), self.decisions, self.uploaded)
        self.by_id = {b["proposal"]: b for b in self.result["breakdown"]}

    def state(self, pid: str) -> str:
        if pid in self.dispositions:
            return self.dispositions[pid]["state"]
        return {"eligible": "RANKED", "flagged": "NEEDS_REVIEW",
                "ineligible": "PROPOSED_INELIGIBLE"}[self.by_id[pid]["status"]]

    def states(self) -> dict[str, str]:
        return {pid: self.state(pid) for pid in self.uploaded}

    UNRESOLVED = ("NEEDS_REVIEW", "PROPOSED_INELIGIBLE")

    def unresolved_ids(self) -> list[str]:
        return sorted(k for k, v in self.states().items() if v in self.UNRESOLVED)

    def provisional(self) -> bool:
        return bool(self.unresolved_ids())

    def critical_unresolved(self) -> list[str]:
        """Unresolved bids that could decide the winner (base case or any plausible configuration)."""
        crit = set(self.result["governance"]["rank_critical_unresolved"])
        return [p for p in self.unresolved_ids() if p in crit]

    def ranked_ids(self) -> list[str]:
        return [b["proposal"] for b in self.result["breakdown"]
                if b["status"] == "eligible" and b["proposal"] not in self.dispositions]

    # ---- guards ---------------------------------------------------------------
    def _refuse(self, actor: str, action: str, why: str):
        self.log.append(actor, f"{action}_refused", {"why": why})
        raise GateError(why)

    @staticmethod
    def _has(reason) -> bool:
        return bool(reason and str(reason).strip())

    def _four_eyes(self, actor, second, action) -> None:
        if not second or second == actor:
            self._refuse(actor, action, "a second, different person must confirm this action")

    def _record(self, actor: str, action: str, payload: dict, makers: tuple = ()) -> None:
        self.maker_actors.update(makers)
        self.log.append(actor, action, payload)

    # ---- human actions --------------------------------------------------------
    def open_evidence(self, actor: str, pid: str) -> None:
        self.opened.setdefault(pid, set()).add(actor)
        self.log.append(actor, "evidence_opened", {"proposal": pid})

    def correct_fact(self, actor: str, pid: str, field: str, value, reason: str,
                     source: str = "reviewer", doc_hash: str | None = None) -> None:
        """source='reviewer': fix an extraction error (quote reviewed). source='clarification':
        fill a gap from a logged addendum. Clarification never overwrites or changes price."""
        if field not in {f["id"] for f in self.criteria["fields"]}:
            self._refuse(actor, "correct_fact", f"unknown field '{field}'")
        if not self._has(reason):
            self._refuse(actor, "correct_fact", "a reason is required")
        cur = self.extractions[pid]["fields"].get(field, {"status": "absent", "value": None})
        if source == "clarification":
            if not doc_hash:
                self._refuse(actor, "correct_fact", "a clarification must reference the addendum document hash")
            if field in self.criteria["governance"]["clarification_locked_fields"]:
                self._refuse(actor, "correct_fact", f"'{field}' cannot be changed by clarification (would be bid repair)")
            if cur.get("status") == "found":
                self._refuse(actor, "correct_fact", "clarification may fill a gap but never change a stated value")
        elif source != "reviewer":
            self._refuse(actor, "correct_fact", f"unknown source '{source}'")
        new = {"value": value, "status": "found", "source": source, "note": reason}
        if doc_hash:
            new["doc_hash"] = doc_hash
        self.extractions[pid]["fields"][field] = new
        self._record(actor, "fact_corrected", {"proposal": pid, "field": field, "before": cur, "after": new,
                                               "source": source}, makers=(actor,))
        self._recompute()

    def clear_flag(self, actor: str, second: str, pid: str, flag_type: str, reason: str) -> None:
        if not self._has(reason):
            self._refuse(actor, "clear_flag", "a reason is required")
        self._four_eyes(actor, second, "clear_flag")
        if flag_type == "pipeline_failure":
            self._refuse(actor, "clear_flag", "a pipeline failure is fixed by re-processing, not by clearing")
        if not any(f["type"] == flag_type for f in self.extractions[pid].get("integrity_flags", [])):
            self._refuse(actor, "clear_flag", f"no '{flag_type}' flag on {pid}")
        self.decisions[pid]["cleared_flags"].append(flag_type)
        self._record(actor, "flag_cleared", {"proposal": pid, "flag": flag_type, "reason": reason,
                                             "second": second}, makers=(actor, second))
        self._recompute()

    def waive_mandatory(self, actor: str, second: str, pid: str, criterion_id: str, reason: str) -> None:
        if not self._has(reason):
            self._refuse(actor, "waive_mandatory", "a reason is required")
        self._four_eyes(actor, second, "waive_mandatory")
        self.decisions[pid]["waive_mandatory"].append(criterion_id)
        self._record(actor, "mandatory_waived", {"proposal": pid, "criterion": criterion_id, "reason": reason,
                                                 "second": second}, makers=(actor, second))
        self._recompute()

    def confirm_exclusion(self, actor: str, second: str, pid: str, reason: str) -> None:
        if self.state(pid) != "PROPOSED_INELIGIBLE":
            self._refuse(actor, "confirm_exclusion", f"{pid} is {self.state(pid)}, not PROPOSED_INELIGIBLE")
        if not self._has(reason):
            self._refuse(actor, "confirm_exclusion", "a reason is required")
        self._four_eyes(actor, second, "confirm_exclusion")
        self.dispositions[pid] = {"state": "EXCLUDED", "reason": reason}
        self._record(actor, "exclusion_confirmed", {"proposal": pid, "reason": reason, "second": second},
                     makers=(actor, second))

    def set_aside(self, actor: str, second: str, pid: str, reason: str) -> None:
        if self.state(pid) not in ("NEEDS_REVIEW", "PROPOSED_INELIGIBLE"):
            self._refuse(actor, "set_aside", f"{pid} is {self.state(pid)}")
        if not self._has(reason):
            self._refuse(actor, "set_aside", "a reason is required")
        self._four_eyes(actor, second, "set_aside")
        self.dispositions[pid] = {"state": "SET_ASIDE", "reason": reason}
        self._record(actor, "bid_set_aside", {"proposal": pid, "reason": reason, "second": second},
                     makers=(actor, second))

    def acknowledge_close_call(self, actor: str, reason: str) -> None:
        if not self._has(reason):
            self._refuse(actor, "acknowledge_close_call", "a reason is required")
        self.close_call_ack = actor
        self._record(actor, "close_call_acknowledged", {"reason": reason}, makers=(actor,))

    # ---- finalisation gate ----------------------------------------------------
    def gate_problems(self, approver: str, award_pid: str, justification: str | None) -> list[str]:
        p = []
        states = self.states()
        open_items = self.unresolved_ids()
        if open_items:
            p.append(f"unresolved bids: {open_items}")
        out_ids = [b["proposal"] for b in self.result["breakdown"]]
        if sorted(out_ids) != sorted(self.uploaded) or len(set(out_ids)) != len(out_ids):
            p.append(f"reconciliation failed: {len(self.uploaded)} in, {len(out_ids)} out")
        ranked = self.ranked_ids()
        need = ranked[: self.criteria["governance"]["top_n_evidence"]] + \
            [k for k, v in states.items() if v == "EXCLUDED"]
        unopened = sorted(k for k in need if not self.opened.get(k))
        if unopened:
            p.append(f"evidence not opened by any reviewer for: {unopened}")
        if self.result["governance"]["close_call"] and not self.close_call_ack:
            p.append("close call (margin below threshold) not acknowledged")
        if approver in self.maker_actors:
            p.append(f"approver '{approver}' took part in the decisions (maker-checker)")
        if award_pid not in ranked:
            p.append(f"{award_pid} is not a ranked bid")
        elif self.by_id[award_pid]["rank"] != 1 and not self._has(justification):
            p.append("awarding a bid that is not rank 1 requires a written justification")
        ok, bad = self.log.verify()
        if not ok:
            p.append(f"audit log failed verification at entry {bad}")
        return p

    def finalise(self, approver: str, award_pid: str, justification: str | None = None) -> dict:
        problems = self.gate_problems(approver, award_pid, justification)
        if problems:
            self.log.append(approver, "finalise_blocked", {"problems": problems})
            raise GateError("; ".join(problems))
        self.finalised = {"approver": approver, "award": award_pid, "justification": justification,
                          "states": self.states(), "criteria_sha256": self.result["run"]["criteria_sha256"]}
        self.log.append(approver, "award_recorded", self.finalised)
        return self.finalised


# ----------------------------------------------------------------------------- demo
def demo() -> str:
    lines: list[str] = []
    say = lines.append
    counter = itertools.count(1)
    log = AuditLog(clock=lambda: f"2026-10-28T09:{next(counter):02d}:00")
    c = score.load_json(HERE / "criteria.json")
    exs = [score.load_json(p) for p in sorted((HERE / "extractions").glob("*.json"))]
    run = Run(c, list("ABCDE"), exs, log=log, started_by="reviewer.r1")

    def show(title):
        say(f"\n--- {title}")
        for pid in run.uploaded:
            b = run.by_id[pid]
            env = (f"  ({score.envelope_text(b['envelope'])} if resolved alone)"
                   if b["envelope"] and pid in run.unresolved_ids() else "")
            rk = f"rank {b['rank']}, {b['score_lo']}" if pid in run.ranked_ids() else ""
            say(f"  {pid}: {run.state(pid):<20} {rk}{env}")
        say(f"  ranking {'PROVISIONAL' if run.provisional() else 'COMPLETE'}; unresolved bids that could decide "
            f"the winner: {run.critical_unresolved() or 'none'}; margin to second: "
            f"{run.result['governance']['margin_to_second']}")

    def attempt(label, fn):
        try:
            fn()
            say(f"  OK       {label}")
        except GateError as e:
            say(f"  REFUSED  {label}\n           -> {e}")

    show("1. After automated processing")
    attempt("approver ap1 tries to record the award now", lambda: run.finalise("ap1", "A"))

    say("\n--- 2. Reviewers work the queue")
    run.open_evidence("r1", "B")
    attempt("r1 confirms B's exclusion alone", lambda: run.confirm_exclusion("r1", None, "B", "licence not held"))
    attempt("r1 + r2 confirm B's exclusion",
            lambda: run.confirm_exclusion("r1", "r2", "B", "electrical work subcontracted; licence not held by bidder"))
    run.open_evidence("r1", "C")
    attempt("r1 + r2 set C aside (procurement-integrity decision)",
            lambda: run.set_aside("r1", "r2", "C", "embedded instruction aimed at automated reviewers; referred to procurement"))
    run.open_evidence("r1", "D")
    attempt("clarification tries to change D's price",
            lambda: run.correct_fact("r1", "D", "total_price_cad", 200000, "bidder asks to lower price",
                                     source="clarification", doc_hash="sha256:addendum-1"))
    attempt("clarification supplies D's missing schedule (15 weeks)",
            lambda: run.correct_fact("r1", "D", "completion_weeks", 15, "addendum 1, requested from all bidders on equal terms",
                                     source="clarification", doc_hash="sha256:addendum-1"))
    show("3. After reviewer decisions (re-scored with no AI call)")

    say("\n--- 4. Approval")
    attempt("r1 (a decision-maker) tries to approve", lambda: run.finalise("r1", "A"))
    attempt("ap1 approves before evidence for A and E was opened", lambda: run.finalise("ap1", "A"))
    run.open_evidence("r2", "A")
    run.open_evidence("r2", "E")
    attempt("ap1 records the award to A", lambda: run.finalise("ap1", "A"))

    ok, bad = log.verify()
    say(f"\naudit log: {len(log.entries)} entries, chain verifies: {ok}")
    tampered = copy.deepcopy(log)
    tampered.entries[6]["payload"]["reason"] = "edited after the fact"
    ok2, bad2 = tampered.verify()
    say(f"after editing entry 7 by hand: chain verifies: {ok2} (first bad entry: {bad2})")
    log.export(HERE / "output" / "demo_audit_log.jsonl")
    return "\n".join(lines)


if __name__ == "__main__":
    text = demo()
    print(text)
    (HERE / "output").mkdir(exist_ok=True)
    (HERE / "output" / "demo_transcript.txt").write_text(text + "\n", encoding="utf-8")
