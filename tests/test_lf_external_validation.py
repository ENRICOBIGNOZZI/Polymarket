#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location(
    "lf_external_validation", SCRIPTS / "lf_external_validation.py"
)
assert SPEC and SPEC.loader
lf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lf
SPEC.loader.exec_module(lf)


class LFExternalValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(
            (ROOT / "config" / "external_intelligence.json").read_text(encoding="utf-8")
        )

    def test_nested_ablation_requires_incremental_external_value_and_clusters_dependence(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["backtest"].update(
            {
                "min_train_observations": 8,
                "extra_cost_bps": 5.0,
                "bootstrap_reps": 200,
                "bootstrap_block": 3,
                "folds": 4,
                "dependence_cluster_seconds": 3600,
            }
        )
        config["gates"].update(
            {
                "min_oos_predictions": 40,
                "min_trades": 30,
                "min_dependence_clusters": 20,
                "max_bootstrap_pvalue": 0.10,
                "min_positive_fold_fraction": 0.75,
            }
        )

        rows = []
        base = 1_800_000_000
        for index in range(60):
            decision_ts = base + index * 7200
            sign = 1.0 if index % 2 == 0 else -1.0
            for market_index in range(2):
                current = 0.50
                target = sign * (0.045 + 0.002 * market_index)
                future_mid = current + target
                rows.append(
                    {
                        "observed_ts": decision_ts,
                        "future_ts": decision_ts + 3600,
                        "market_id": f"m-{market_index}",
                        "event_id": "shared-event",
                        "pm_mid": current,
                        "pm_bid": 0.495,
                        "pm_ask": 0.505,
                        "future_mid": future_mid,
                        "future_bid": future_mid - 0.005,
                        "future_ask": future_mid + 0.005,
                        "target_delta": target,
                        "q_external": current + sign * 0.08,
                        "feature_value": sign * 0.08,
                    }
                )
        rows.sort(key=lambda row: (row["observed_ts"], row["market_id"]))

        report = lf.nested_short_horizon_ablation(
            rows, config, "kalshi", "external_probability", 3600
        )
        self.assertTrue(report["gate_pass"], report["reasons"])
        self.assertGreater(report["incremental_mse_improvement"], 0.0)
        self.assertGreater(report["incremental_cost_stress_pnl_per_share"]["2.0"], 0.0)
        self.assertLessEqual(report["cluster_bootstrap_pvalue"], 0.10)
        self.assertLess(report["dependence_clusters"], report["oos_predictions"])
        self.assertTrue(report["requires_incumbent_champion_ablation_before_integration"])
        self.assertFalse(report["production_change"])

    def test_no_external_drift_cannot_be_mislabeled_as_incremental_alpha(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["backtest"].update(
            {
                "min_train_observations": 6,
                "extra_cost_bps": 0.0,
                "bootstrap_reps": 100,
                "dependence_cluster_seconds": 3600,
            }
        )
        config["gates"].update(
            {
                "min_oos_predictions": 10,
                "min_trades": 1,
                "min_dependence_clusters": 5,
                "max_bootstrap_pvalue": 0.10,
                "min_positive_fold_fraction": 0.50,
            }
        )

        rows = []
        base = 1_810_000_000
        for index in range(40):
            decision_ts = base + index * 7200
            rows.append(
                {
                    "observed_ts": decision_ts,
                    "future_ts": decision_ts + 3600,
                    "market_id": "drift-market",
                    "event_id": "drift-event",
                    "pm_mid": 0.50,
                    "pm_bid": 0.495,
                    "pm_ask": 0.505,
                    "future_mid": 0.53,
                    "future_bid": 0.525,
                    "future_ask": 0.535,
                    "target_delta": 0.03,
                    "q_external": 0.50,
                    "feature_value": 0.0,
                }
            )

        report = lf.nested_short_horizon_ablation(
            rows, config, "kalshi", "external_probability", 3600
        )
        self.assertFalse(report["gate_pass"])
        self.assertLessEqual(report["incremental_mse_improvement"], 1e-12)
        self.assertIn(
            "no_incremental_mse_improvement_vs_no_external", report["reasons"]
        )
        self.assertLessEqual(
            report["incremental_cost_stress_pnl_per_share"]["1.0"], 1e-12
        )

    def test_terminal_probability_scores_resolved_markets_without_pseudoreplication(self) -> None:
        observations = []
        prices = []
        base = 1_820_000_000
        outcomes = {"m1": 1, "m2": 0, "m3": 1, "m4": 0}
        events = {"m1": "e1", "m2": "e1", "m3": "e2", "m4": "e2"}
        for market_id, outcome in outcomes.items():
            end_ts = base + 4 * 86400
            pm_q = 0.55 if outcome else 0.45
            external_q = 0.85 if outcome else 0.15
            for offset in (0, 3600):
                observations.append(
                    {
                        "observed_ts": base + offset,
                        "market_id": market_id,
                        "event_id": events[market_id],
                        "end_ts": end_ts,
                        "source": "kalshi",
                        "feature_name": "external_probability",
                        "q_external": external_q,
                        "pm_mid": pm_q,
                    }
                )
            prices.append(
                {
                    "observed_ts": end_ts,
                    "market_id": market_id,
                    "resolved_outcome": outcome,
                    "mid": float(outcome),
                }
            )

        report = lf.terminal_probability_ablation(observations, prices)
        overall = report["overall"]
        self.assertEqual(overall["market_bucket_observations"], 4)
        self.assertEqual(overall["unique_markets"], 4)
        self.assertEqual(overall["event_clusters"], 2)
        self.assertGreater(overall["brier_improvement_external_vs_polymarket"], 0.0)
        self.assertGreater(overall["log_loss_improvement_external_vs_polymarket"], 0.0)
        self.assertGreater(overall["event_mean_brier_improvement"], 0.0)
        self.assertIn("1-7d", report["by_time_to_resolution"])
        self.assertFalse(report["production_change"])


if __name__ == "__main__":
    unittest.main()
