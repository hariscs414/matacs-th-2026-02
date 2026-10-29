"""Workflow-layer tests: the human controls and the audit trail. Run: python -m unittest -v"""
import copy
import itertools
import unittest
from pathlib import Path

import score
import workflow
from workflow import GateError, Run, AuditLog

HERE = Path(__file__).resolve().parent
CRITERIA = score.load_json(HERE / "criteria.json")
EXS = [score.load_json(p) for p in sorted((HERE / "extractions").glob("*.json"))]


def fresh(uploaded="ABCDE", criteria=None, exs=None):
    n = itertools.count(1)
    return Run(criteria or CRITERIA, list(uploaded), exs or EXS, log=AuditLog(clock=lambda: f"t{next(n)}"))


def resolve_everything(run):
    """A legitimate path to a finalisable run."""
    run.confirm_exclusion("r1", "r2", "B", "licence not held by bidder")
    run.set_aside("r1", "r2", "C", "integrity: referred to procurement")
    run.correct_fact("r1", "D", "completion_weeks", 15, "addendum 1", source="clarification", doc_hash="sha256:x")
    for pid in "ABCDE":
        run.open_evidence("r2", pid)


class States(unittest.TestCase):
    def test_every_upload_has_exactly_one_visible_state(self):
        run = fresh()
        self.assertEqual(run.states(), {"A": "RANKED", "B": "PROPOSED_INELIGIBLE", "C": "NEEDS_REVIEW",
                                        "D": "NEEDS_REVIEW", "E": "RANKED"})
        self.assertEqual((run.result["run"]["proposals_in"], run.result["run"]["proposals_out"]), (5, 5))

    def test_failed_extraction_is_needs_review_not_dropped_and_not_clearable(self):
        run = fresh("ABCDEF")                       # F was uploaded but the pipeline produced nothing
        self.assertEqual(run.state("F"), "NEEDS_REVIEW")
        with self.assertRaises(GateError):
            run.clear_flag("r1", "r2", "F", "pipeline_failure", "looks fine")

    def test_provisional_until_every_bid_has_an_outcome(self):
        run = fresh()
        self.assertTrue(run.provisional())
        self.assertEqual(run.critical_unresolved(), ["D"])     # the flag that could decide the winner
        resolve_everything(run)
        self.assertFalse(run.provisional())


class HumanControls(unittest.TestCase):
    def test_exclusion_needs_two_different_people_and_a_reason(self):
        run = fresh()
        for second in (None, "r1"):
            with self.assertRaises(GateError):
                run.confirm_exclusion("r1", second, "B", "licence not held")
        with self.assertRaises(GateError):
            run.confirm_exclusion("r1", "r2", "B", "  ")
        run.confirm_exclusion("r1", "r2", "B", "licence not held")
        self.assertEqual(run.state("B"), "EXCLUDED")

    def test_cannot_confirm_exclusion_of_a_bid_the_engine_did_not_propose_for_exclusion(self):
        with self.assertRaises(GateError):
            fresh().confirm_exclusion("r1", "r2", "A", "dislike")

    def test_clarification_fills_gaps_but_never_changes_stated_values_or_price(self):
        run = fresh()
        with self.assertRaises(GateError):   # price is locked
            run.correct_fact("r1", "D", "total_price_cad", 1, "x", source="clarification", doc_hash="h")
        with self.assertRaises(GateError):   # already stated: cannot be changed by clarification
            run.correct_fact("r1", "A", "completion_weeks", 8, "x", source="clarification", doc_hash="h")
        with self.assertRaises(GateError):   # must reference the addendum
            run.correct_fact("r1", "D", "completion_weeks", 15, "x", source="clarification")
        run.correct_fact("r1", "D", "completion_weeks", 15, "addendum", source="clarification", doc_hash="h")
        self.assertEqual((run.state("D"), run.by_id["D"]["rank"], run.by_id["D"]["score_lo"]), ("RANKED", 3, 63.9))

    def test_a_fast_enough_clarified_schedule_moves_D_ahead_of_E(self):
        run = fresh()
        run.correct_fact("r1", "D", "completion_weeks", 14, "addendum", source="clarification", doc_hash="h")
        self.assertEqual((run.by_id["D"]["rank"], run.by_id["E"]["rank"]), (2, 3))

    def test_rescoring_after_decisions_makes_no_ai_call(self):
        run = fresh()   # nothing in workflow.py can call a model: guard against regressions
        self.assertNotIn("anthropic", (HERE / "workflow.py").read_text(encoding="utf-8"))
        run.correct_fact("r1", "D", "completion_weeks", 15, "addendum", source="clarification", doc_hash="h")

    def test_overturning_B_needs_four_eyes_and_changes_the_ranking_visibly(self):
        run = fresh()
        with self.assertRaises(GateError):
            run.waive_mandatory("r1", None, "B", "licence", "ok")
        run.waive_mandatory("r1", "r2", "B", "licence", "client accepts licensed subcontractor")
        self.assertEqual((run.by_id["B"]["rank"], run.by_id["E"]["rank"]), (2, 3))


class FinalisationGate(unittest.TestCase):
    def test_blocked_while_unresolved_and_the_refusal_is_logged(self):
        run = fresh()
        with self.assertRaises(GateError) as cm:
            run.finalise("ap1", "A")
        self.assertIn("unresolved", str(cm.exception))
        self.assertEqual(run.log.entries[-1]["action"], "finalise_blocked")

    def test_blocked_until_evidence_opened_and_approver_independent(self):
        run = fresh()
        run.confirm_exclusion("r1", "r2", "B", "licence not held")
        run.set_aside("r1", "r2", "C", "integrity")
        run.correct_fact("r1", "D", "completion_weeks", 15, "addendum", source="clarification", doc_hash="h")
        with self.assertRaises(GateError) as cm:
            run.finalise("ap1", "A")
        self.assertIn("evidence not opened", str(cm.exception))
        for pid in "ABE":
            run.open_evidence("r2", pid)
        with self.assertRaises(GateError) as cm:
            run.finalise("r2", "A")                       # r2 helped decide: maker-checker
        self.assertIn("maker-checker", str(cm.exception))
        self.assertEqual(run.finalise("ap1", "A")["award"], "A")

    def test_awarding_a_non_winner_needs_a_written_justification(self):
        run = fresh()
        resolve_everything(run)
        with self.assertRaises(GateError):
            run.finalise("ap1", "E")
        self.assertEqual(run.finalise("ap1", "E", "A fails a site-access condition not captured by criteria")["award"], "E")

    def test_cannot_award_an_excluded_or_set_aside_bid(self):
        run = fresh()
        resolve_everything(run)
        for pid in "BC":
            with self.assertRaises(GateError):
                run.finalise("ap1", pid, "because")

    def test_close_call_must_be_acknowledged(self):
        c = copy.deepcopy(CRITERIA)
        c["governance"]["close_call_margin"] = 20.0       # A leads E by 13.7: now a "close call"
        run = fresh(criteria=c)
        resolve_everything(run)
        with self.assertRaises(GateError) as cm:
            run.finalise("ap1", "A")
        self.assertIn("close call", str(cm.exception))
        run.acknowledge_close_call("r2", "margin reviewed with panel")
        with self.assertRaises(GateError):               # r2 acknowledged it, so r2 cannot approve
            run.finalise("r2", "A")
        run.finalise("ap1", "A")

    def test_complete_legitimate_path_succeeds(self):
        run = fresh()
        resolve_everything(run)
        out = run.finalise("ap1", "A")
        self.assertEqual(out["states"], {"A": "RANKED", "B": "EXCLUDED", "C": "SET_ASIDE", "D": "RANKED", "E": "RANKED"})


class AuditLogIntegrity(unittest.TestCase):
    def test_chain_verifies_and_any_edit_is_detected(self):
        run = fresh()
        resolve_everything(run)
        run.finalise("ap1", "A")
        self.assertEqual(run.log.verify(), (True, None))
        for victim in (0, 3, len(run.log.entries) - 1):
            log = copy.deepcopy(run.log)
            log.entries[victim]["actor"] = "someone.else"
            self.assertEqual(log.verify(), (False, victim + 1))

    def test_deleting_or_reordering_entries_is_detected(self):
        run = fresh()
        resolve_everything(run)
        dropped = copy.deepcopy(run.log)
        del dropped.entries[4]
        self.assertFalse(dropped.verify()[0])
        swapped = copy.deepcopy(run.log)
        swapped.entries[2], swapped.entries[3] = swapped.entries[3], swapped.entries[2]
        self.assertFalse(swapped.verify()[0])

    def test_log_records_who_what_before_after_and_reason(self):
        run = fresh()
        run.correct_fact("r1", "D", "completion_weeks", 15, "addendum 1", source="clarification", doc_hash="h")
        e = run.log.entries[-1]
        self.assertEqual((e["actor"], e["action"]), ("r1", "fact_corrected"))
        self.assertEqual(e["payload"]["before"]["status"], "ambiguous")
        self.assertEqual(e["payload"]["after"]["value"], 15)
        self.assertEqual(e["payload"]["after"]["note"], "addendum 1")

    def test_a_tampered_log_blocks_the_award(self):
        run = fresh()
        resolve_everything(run)
        run.log.entries[2]["payload"]["reason"] = "rewritten"
        with self.assertRaises(GateError) as cm:
            run.finalise("ap1", "A")
        self.assertIn("audit log failed verification", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
