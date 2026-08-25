#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_v6_graph_rv_completion_frontier as mod  # noqa: E402


CURRENT = {
    "candidate": {
        "bundle_id": "GRAPH_HARD-1787649330-1",
        "event_id": "837321",
        "strategy": "GRAPH_RV",
        "mode": "MAKER",
        "limit_prices": [0.33, 0.36, 0.29],
        "reported_expected_edge": 0.02,
    },
    "policy": {
        "guard_stress_bps": 10.0,
        "min_edge": 0.0002,
        "min_candidate_sessions": 12,
        "min_candidate_completions": 10,
    },
    "candidate_specific_execution": {
        "sessions": 0,
        "completed_sessions": 0,
    },
}


class GraphRVCompletionFrontierTest(unittest.TestCase):
    def test_current_live_candidate_is_static_positive_but_not_execution_ready(self) -> None:
        out = mod.analyze(copy.deepcopy(CURRENT))
        self.assertAlmostEqual(out["limit_price_sum"], 0.98, places=12)
        self.assertAlmostEqual(out["quoted_maker_edge_per_share"], 0.02, places=12)
        self.assertAlmostEqual(out["guard_stressed_edge_per_share"], 0.019, places=12)
        self.assertTrue(out["static_guard_pass"])
        self.assertFalse(out["static_guard_is_execution_evidence"])
        self.assertFalse(out["hard_arb_claim"])
        self.assertEqual(out["evidence_state"], "MORE_EVIDENCE_REQUIRED")
        self.assertIn("missing_candidate_specific_completion_abort_economics", out["reasons"])

    def test_binary_book_complete_set_history_is_not_implicitly_transferred(self) -> None:
        payload = copy.deepcopy(CURRENT)
        payload["context_only_nontransferable_evidence"] = {
            "strategy_class": "binary_yes_no_complete_set",
            "sessions": 24,
            "probes_per_policy": 352,
            "paired_fills": 0,
            "transferable_to_graph_rv": False,
        }
        out = mod.analyze(payload)
        self.assertEqual(out["candidate_specific_sessions"], 0)
        self.assertEqual(out["evidence_state"], "MORE_EVIDENCE_REQUIRED")

    def test_strong_candidate_specific_execution_can_reach_governance_review(self) -> None:
        payload = copy.deepcopy(CURRENT)
        payload["candidate_specific_execution"] = {
            "sessions": 24,
            "completed_sessions": 23,
            "mean_complete_net_pnl_per_share": 0.018,
            "mean_abort_net_pnl_per_share": -0.008,
            "independent_positive_windows": 3,
            "cost_stress_expected_net_pnl_per_share": {
                "1x": 0.010,
                "1.5x": 0.007,
                "2x": 0.004,
            },
        }
        out = mod.analyze(payload)
        self.assertGreater(out["completion_rate_wilson_lower_one_sided_95"], 0.75)
        self.assertGreater(out["conservative_expected_net_pnl_per_share"], 0.0)
        self.assertEqual(out["evidence_state"], "EVIDENCE_READY_FOR_GOVERNANCE_REVIEW")

    def test_completion_downside_rejects_static_edge(self) -> None:
        payload = copy.deepcopy(CURRENT)
        payload["candidate_specific_execution"] = {
            "sessions": 24,
            "completed_sessions": 10,
            "mean_complete_net_pnl_per_share": 0.018,
            "mean_abort_net_pnl_per_share": -0.030,
            "independent_positive_windows": 4,
            "cost_stress_expected_net_pnl_per_share": {
                "1x": 0.001,
                "1.5x": 0.001,
                "2x": 0.001,
            },
        }
        out = mod.analyze(payload)
        self.assertLessEqual(out["conservative_expected_net_pnl_per_share"], 0.0)
        self.assertEqual(out["evidence_state"], "REJECTED")
        self.assertIn("completion_lower_bound_does_not_cover_abort_downside", out["reasons"])

    def test_positive_normal_execution_but_nonpositive_2x_is_rejected(self) -> None:
        payload = copy.deepcopy(CURRENT)
        payload["candidate_specific_execution"] = {
            "sessions": 24,
            "completed_sessions": 23,
            "mean_complete_net_pnl_per_share": 0.018,
            "mean_abort_net_pnl_per_share": -0.008,
            "independent_positive_windows": 3,
            "cost_stress_expected_net_pnl_per_share": {
                "1x": 0.010,
                "1.5x": 0.004,
                "2x": 0.0,
            },
        }
        out = mod.analyze(payload)
        self.assertEqual(out["evidence_state"], "REJECTED")
        self.assertIn("nonpositive_cost_stressed_execution_economics", out["reasons"])

    def test_reported_edge_must_match_leg_prices(self) -> None:
        payload = copy.deepcopy(CURRENT)
        payload["candidate"]["reported_expected_edge"] = 0.03
        with self.assertRaises(ValueError):
            mod.analyze(payload)


if __name__ == "__main__":
    unittest.main()
