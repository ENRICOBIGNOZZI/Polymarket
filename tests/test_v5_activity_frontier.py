#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v5_activity_frontier", ROOT / "scripts" / "v5_activity_frontier.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class V5ActivityFrontierTest(unittest.TestCase):
    def test_parse_funnel_counts(self) -> None:
        row = MODULE.parse_kv_log(
            "pca_stat_arb discovered=240 panel_series=31 factors=3 explained=0.68928 "
            "mr_pass=28 relation_pass=11 hedge_pass=1 opportunities=1 raw_positive=1 "
            "taker_positive=0 maker_entry_positive=0"
        )
        self.assertEqual(row["discovered"], 240)
        self.assertEqual(row["hedge_pass"], 1)
        self.assertEqual(row["maker_entry_positive"], 0)
        self.assertAlmostEqual(row["explained"], 0.68928)

    def test_negative_post_cost_edge_is_execution_bound(self) -> None:
        row = MODULE.classify_candidate(
            {
                "market": "559655",
                "side": "NO",
                "raw_expected_edge": "4.19995e-05",
                "maker_entry_net_edge": "-0.00176914",
                "taker_net_edge": "-0.00515719",
            }
        )
        self.assertEqual(row["failure"], "execution_cost_bound")
        self.assertGreater(row["maker_cost_wedge"], row["raw_edge"])
        self.assertGreater(row["maker_required_raw_multiple"], 40.0)
        self.assertGreater(row["taker_required_raw_multiple"], 120.0)

    def test_threshold_relaxation_cannot_rescue_negative_post_cost_sign(self) -> None:
        snapshot = {
            "git_sha": "abc",
            "generated_ts": 1,
            "candidates": {
                "b1": [],
                "b2": [
                    {
                        "market": "m",
                        "side": "YES",
                        "raw_expected_edge": 0.0002,
                        "maker_entry_net_edge": -0.0003,
                        "taker_net_edge": -0.0008,
                    }
                ],
            },
            "logs": {
                "b1": ["discovered=100 model_fits=0 opportunities=0 raw_positive=0"],
                "b2": ["discovered=100 mr_pass=8 hedge_pass=1 opportunities=1 raw_positive=1 maker_entry_positive=0"],
            },
        }
        config = {
            "market_limit": 300,
            "min_liquidity": 100.0,
            "multi_strategy": {"strategies": []},
        }
        report = MODULE.analyze(snapshot, config)
        self.assertEqual(report["decision"], "REJECT_THRESHOLD_RELAXATION_CURRENT_SAMPLE")
        self.assertFalse(
            report["hypothesis_test"]["lower_thresholds_can_rescue_current_raw_positive_rows"]
        )
        self.assertEqual(report["candidate_counts"]["execution_cost_bound"], 1)

    def test_post_cost_positive_row_keeps_experiment_open(self) -> None:
        snapshot = {
            "candidates": {
                "b1": [
                    {
                        "market": "m",
                        "side": "YES",
                        "raw_expected_edge": 0.003,
                        "maker_entry_net_edge": 0.001,
                        "taker_net_edge": -0.001,
                    }
                ],
                "b2": [],
            },
            "logs": {"b1": ["opportunities=1 raw_positive=1"], "b2": [""]},
        }
        report = MODULE.analyze(snapshot, {"multi_strategy": {"strategies": []}})
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertEqual(report["candidate_counts"]["post_cost_positive"], 1)


if __name__ == "__main__":
    unittest.main()
