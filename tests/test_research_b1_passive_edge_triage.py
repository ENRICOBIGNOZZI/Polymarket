from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "scripts").resolve()))

from build_v4_intents import b1_relation_valid  # noqa: E402
from research_b1_passive_edge_triage import (  # noqa: E402
    analyze_snapshots,
    counterfactual_break_even_completion,
)


def snapshot(*rows):
    return {"candidates": {"b1": list(rows)}}


def row(
    *,
    maker=0.0296454,
    taker=-0.0026806,
    raw=0.037133,
    stability=0.464128,
    y_market="3535940",
    x_market="3535928",
    y_side="YES",
    x_side="NO",
):
    return {
        "relation": "same_event",
        "y_market": y_market,
        "x_market": x_market,
        "y_side": y_side,
        "x_side": x_side,
        "maker_entry_net_edge": maker,
        "taker_net_edge": taker,
        "raw_expected_edge": raw,
        "stability": stability,
    }


class B1PassiveEdgeTriageTest(unittest.TestCase):
    def test_current_exact_score_candidate_has_low_counterfactual_hurdle(self):
        hurdle = counterfactual_break_even_completion(0.0296454, -0.0026806)
        self.assertIsNotNone(hurdle)
        self.assertAlmostEqual(hurdle, 0.08292396213574213, places=12)

    def test_single_snapshot_prioritizes_replay_but_never_promotes(self):
        report = analyze_snapshots([snapshot(row())])
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertEqual(report["priority_replay_count"], 1)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["action"], "PRIORITIZE_CANDIDATE_SPECIFIC_REPLAY")
        self.assertEqual(candidate["evidence_state"], "MORE_EVIDENCE_REQUIRED")
        self.assertEqual(candidate["recurring_maker_positive_snapshots"], 1)
        self.assertIn("single_snapshot_only", candidate["flags"])
        self.assertIn("marginal_parameter_stability", candidate["flags"])
        self.assertIn("maker_dependent_edge", candidate["flags"])

    def test_recurrence_is_counted_without_turning_into_promotion(self):
        report = analyze_snapshots([snapshot(row()), snapshot(row(stability=0.70))])
        candidate = report["candidates"][0]
        self.assertEqual(candidate["recurring_maker_positive_snapshots"], 2)
        self.assertNotIn("single_snapshot_only", candidate["flags"])
        self.assertEqual(candidate["action"], "PRIORITIZE_CANDIDATE_SPECIFIC_REPLAY")
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")

    def test_nonpositive_maker_edge_is_rejected_even_with_positive_raw_edge(self):
        report = analyze_snapshots([snapshot(row(maker=-0.0001, taker=-0.001, raw=0.03))])
        self.assertEqual(report["candidates"][0]["action"], "REJECT_NONPOSITIVE_MAKER_EDGE")
        self.assertEqual(report["priority_replay_count"], 0)

    def test_positive_taker_edge_is_not_maker_dependent(self):
        report = analyze_snapshots([snapshot(row(maker=0.01, taker=0.002, raw=0.012, stability=0.8))])
        candidate = report["candidates"][0]
        self.assertEqual(candidate["action"], "PRIORITIZE_EXECUTABLE_REPLAY")
        self.assertEqual(candidate["counterfactual_break_even_completion"], 0.0)
        self.assertNotIn("maker_dependent_edge", candidate["flags"])

    def test_malformed_rows_fail_closed(self):
        bad = row()
        del bad["x_market"]
        with self.assertRaises(ValueError):
            analyze_snapshots([snapshot(bad)])

    def test_generic_crypto_template_overlap_cannot_admit_semantic_pair(self):
        candidate = {
            "relation": "semantic",
            "y_slug": "will-ethereum-dip-to-1700-in-august-2026",
            "x_slug": "will-bitcoin-dip-to-60k-in-august-2026",
        }
        self.assertFalse(b1_relation_valid(candidate))

    def test_same_asset_threshold_ladder_keeps_specific_anchor(self):
        candidate = {
            "relation": "semantic",
            "y_slug": "will-bitcoin-dip-to-55k-in-august-2026",
            "x_slug": "will-bitcoin-dip-to-60k-in-august-2026",
        }
        self.assertTrue(b1_relation_valid(candidate))

    def test_same_event_and_latent_correlation_do_not_need_text_anchor(self):
        self.assertTrue(b1_relation_valid({"relation": "same_event"}))
        self.assertTrue(b1_relation_valid({"relation": "latent_corr"}))

    def test_unknown_relation_fails_closed(self):
        self.assertFalse(b1_relation_valid({"relation": "weak", "y_slug": "a", "x_slug": "a"}))


if __name__ == "__main__":
    unittest.main()
