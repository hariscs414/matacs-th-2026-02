"""Scoring-engine tests for the properties claimed in WRITEUP.md. Run: python -m unittest -v"""
import copy
import json
import random
import unittest
from pathlib import Path

import score

HERE = Path(__file__).resolve().parent
CRITERIA = score.load_json(HERE / "criteria.json")
EXTRACTIONS = {p.stem: score.load_json(p) for p in sorted((HERE / "extractions").glob("*.json"))}


def run(extractions=None, criteria=None, **kw):
    kw.setdefault("with_robustness", False)
    return score.run(criteria or CRITERIA, list(extractions or EXTRACTIONS.values()), **kw)


def by_id(result):
    return {r["proposal"]: r for r in result["ranking"]}


class Determinism(unittest.TestCase):
    def test_same_input_same_output(self):
        self.assertEqual(json.dumps(run(), sort_keys=True), json.dumps(run(), sort_keys=True))

    def test_order_of_upload_does_not_matter(self):
        items = list(EXTRACTIONS.values())
        random.Random(7).shuffle(items)
        self.assertEqual(run(items)["ranking"], run()["ranking"])

    def test_score_independent_of_other_proposals(self):
        alone = score.evaluate(CRITERIA, EXTRACTIONS["A"])["score_lo"]
        in_set = next(r for r in run()["breakdown"] if r["proposal"] == "A")["score_lo"]
        self.assertEqual(alone, in_set)


class SampleSet(unittest.TestCase):
    def test_outcomes(self):
        r = by_id(run())
        self.assertEqual((r["A"]["status"], r["A"]["rank"], r["A"]["score"]), ("ranked", 1, 78.1))
        self.assertEqual((r["E"]["status"], r["E"]["rank"], r["E"]["score"]), ("ranked", 2, 64.4))
        self.assertEqual(r["B"]["status"], "ineligible")
        self.assertEqual(r["C"]["status"], "flagged")
        self.assertEqual(r["D"]["status"], "flagged")
        for k in "BCD":
            self.assertIsNone(r[k]["rank"])
            self.assertIsNone(r[k]["score"])

    def test_injection_cannot_change_a_score(self):
        with_flag = score.evaluate(CRITERIA, EXTRACTIONS["C"])
        clean = copy.deepcopy(EXTRACTIONS["C"])
        clean["integrity_flags"] = []
        without = score.evaluate(CRITERIA, clean)
        self.assertEqual(with_flag["score_lo"], without["score_lo"])
        self.assertEqual((with_flag["status"], without["status"]), ("flagged", "eligible"))

    def test_missing_fact_is_a_range_never_a_guess(self):
        d = score.evaluate(CRITERIA, EXTRACTIONS["D"])
        self.assertEqual((d["score_lo"], d["score_hi"]), (61.4, 76.4))
        self.assertTrue(any(f["type"] == "missing_info" for f in d["flags"]))

    def test_rank_envelopes_say_how_much_each_flag_matters(self):
        b = {x["proposal"]: x for x in run()["breakdown"]}
        self.assertEqual(b["D"]["envelope"], {"best": 2, "worst": 3})
        self.assertEqual(b["C"]["envelope"], {"best": 3, "worst": 3})
        self.assertEqual(b["B"]["envelope"], {"best": 2, "worst": 2})  # decisive gate: B beats E if overturned

    def test_robustness_winner_stable_and_D_is_the_one_that_matters(self):
        g = score.run(CRITERIA, list(EXTRACTIONS.values()))["governance"]
        rob = g["robustness"]
        self.assertEqual((rob["winner"], rob["winner_stable_pct"]), ("A", 100.0))
        self.assertGreater(rob["unresolved_could_win_pct"]["D"], 0)
        self.assertEqual(rob["unresolved_could_win_pct"]["C"], 0)
        self.assertEqual(g["rank_critical_unresolved"], ["D"])
        self.assertFalse(g["close_call"])


class FailClosed(unittest.TestCase):
    def test_unverified_mandatory_is_flagged_not_excluded(self):
        ex = copy.deepcopy(EXTRACTIONS["A"])
        ex["fields"]["licence_held_by_bidder"] = {"value": None, "status": "ungrounded"}
        self.assertEqual(score.evaluate(CRITERIA, ex)["status"], "flagged")

    def test_readers_disagreeing_is_flagged_not_excluded(self):
        ex = copy.deepcopy(EXTRACTIONS["A"])
        ex["fields"]["licence_held_by_bidder"] = {"value": None, "status": "disputed"}
        self.assertEqual(score.evaluate(CRITERIA, ex)["status"], "flagged")

    def test_limit_breach_is_flagged(self):
        ex = copy.deepcopy(EXTRACTIONS["A"])
        ex["fields"]["total_price_cad"]["value"] = 270000
        self.assertEqual(score.evaluate(CRITERIA, ex)["status"], "flagged")

    def test_uploaded_file_without_extraction_is_never_dropped(self):
        r = by_id(run(uploaded_ids=list("ABCDEF")))
        self.assertEqual(r["F"]["status"], "flagged")
        self.assertIsNone(r["F"]["rank"])
        self.assertEqual(len(r), 6)

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            score.run(CRITERIA, [EXTRACTIONS["A"], EXTRACTIONS["A"]])


class Policy(unittest.TestCase):
    def test_on_missing_ineligible_makes_missing_schedule_non_responsive(self):
        c = copy.deepcopy(CRITERIA)
        c["weighted"][2]["on_missing"] = "ineligible"          # the live-call change: timeline now required
        r = by_id(run(criteria=c))
        self.assertEqual(r["D"]["status"], "ineligible")
        self.assertEqual((r["A"]["rank"], r["E"]["rank"]), (1, 2))

    def test_disputed_facts_are_still_flagged_even_when_missing_means_ineligible(self):
        c = copy.deepcopy(CRITERIA)
        c["weighted"][2]["on_missing"] = "ineligible"
        ex = copy.deepcopy(EXTRACTIONS["D"])
        ex["fields"]["completion_weeks"] = {"value": None, "status": "disputed"}
        self.assertEqual(score.evaluate(c, ex)["status"], "flagged")   # pipeline doubt never excludes

    def test_recorded_waiver_and_cleared_flag_change_status_and_are_noted(self):
        b = score.evaluate(CRITERIA, EXTRACTIONS["B"], {"waive_mandatory": ["licence"]})
        self.assertEqual(b["status"], "eligible")
        self.assertTrue(any("waived" in n for n in b["notes"]))
        c = score.evaluate(CRITERIA, EXTRACTIONS["C"], {"cleared_flags": ["instruction_to_automated_system"]})
        self.assertEqual(c["status"], "eligible")

    def test_ties_resolved_by_configured_tie_breakers(self):
        twin = copy.deepcopy(EXTRACTIONS["E"])
        twin["proposal_id"] = "E2"
        twin["fields"]["total_price_cad"]["value"] = 241000
        flat = copy.deepcopy(CRITERIA)
        flat["weighted"][1]["zero_at"], flat["weighted"][1]["full_at"] = 0, 1   # price saturates for everyone
        r = by_id(run(list(EXTRACTIONS.values()) + [twin], criteria=flat))
        self.assertLess(r["E2"]["rank"], r["E"]["rank"])


class CriteriaChanges(unittest.TestCase):
    def test_lint_accepts_the_shipped_config(self):
        errors, warnings = score.lint_criteria(CRITERIA)
        self.assertEqual((errors, warnings), ([], []))

    def test_lint_rejects_bad_edits(self):
        def bad(mutate):
            c = copy.deepcopy(CRITERIA)
            mutate(c)
            return score.lint_criteria(c)[0]
        self.assertTrue(bad(lambda c: c["weighted"][0].update(weight=31)))                    # weights != 100
        self.assertTrue(bad(lambda c: c["weighted"][1].update(full_at=260000)))               # zero_at == full_at
        self.assertTrue(bad(lambda c: c["weighted"][0].update(field="made_up_field")))        # unknown field
        self.assertTrue(bad(lambda c: c["weighted"][2].update(on_missing="maybe")))           # invalid policy
        with self.assertRaises(ValueError):
            score.run(self._edited(lambda c: c["weighted"][0].update(weight=31)), list(EXTRACTIONS.values()))

    def _edited(self, mutate):
        c = copy.deepcopy(CRITERIA)
        mutate(c)
        return c

    def test_new_version_has_new_hash_old_version_untouched_and_diff_is_shown(self):
        v2 = self._edited(lambda c: (c.update(criteria_version="v2"),
                                     c["weighted"][0].update(weight=20), c["weighted"][1].update(weight=35)))
        a, b = run(), run(criteria=v2)
        self.assertNotEqual(a["run"]["criteria_sha256"], b["run"]["criteria_sha256"])
        self.assertEqual(run()["run"]["criteria_sha256"], a["run"]["criteria_sha256"])
        diff = {d["proposal"]: d for d in score.compare_runs(a, b)}
        self.assertNotEqual(diff["A"]["old"][2], diff["A"]["new"][2])    # score moved, shown to the reviewer

    def test_preview_probes_each_anchor(self):
        rows = score.preview(CRITERIA)
        self.assertTrue(all(r["sub_scores"] == [0.0, 50.0, 100.0] for r in rows))


if __name__ == "__main__":
    unittest.main()
