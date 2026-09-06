from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/research_v7_btc_m5_external_cancel_forward.py"
SPEC = importlib.util.spec_from_file_location("research_v7_btc_m5_external_cancel_forward", MODULE_PATH)
assert SPEC and SPEC.loader
forward = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(forward)

REGISTRY = ROOT / "config/v7_maker_fillability_experiments.json"
MODEL_SHA = "a" * 40


class ExternalCancelForwardEvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        experiment = forward.load_experiment(REGISTRY, forward.DEFAULT_EXPERIMENT_ID)
        cls.experiment = experiment
        cls.rule_hash = forward.canonical_rule_hash(experiment["frozen_rule"])
        cls.freeze_ms = forward.utc_ms(experiment["start_time"])

    def episode(
        self, market: int, index: int, *,
        primary_improvement: float = 0.10,
        stress_improvement: float = 0.05,
        avoid_fill: bool = True,
    ) -> dict:
        quote_ms = self.freeze_ms + 1_000 + market * 10_000 + index
        baseline_500 = -primary_improvement
        overlay_fill = not avoid_fill
        overlay_500 = 0.0 if overlay_fill else None
        return {
            "schema": forward.EPISODE_SCHEMA,
            "market_id": f"m{market:03d}",
            "quote_id": f"m{market:03d}-q{index}",
            "quote_receive_ms": quote_ms,
            "maker_model_published_ms": self.freeze_ms - 1_000,
            "maker_model_sha": MODEL_SHA,
            "rule_sha256": self.rule_hash,
            "book_tape_schema": 2,
            "receive_time_causal": True,
            "causality_violations": [],
            "trigger_applied": True,
            "quote_size_shares": 5.0,
            "baseline_fill": True,
            "overlay_fill": overlay_fill,
            "baseline_markout_per_share": {
                "250": baseline_500 * 0.8,
                "500": baseline_500,
                "1000": baseline_500 * 0.7,
            },
            "overlay_markout_per_share": (
                {"250": 0.0, "500": overlay_500, "1000": 0.0}
                if overlay_fill else {}
            ),
            "stress": {
                forward.STRESS_KEY: {
                    "baseline_fill": True,
                    "overlay_fill": stress_improvement < 0.0,
                    "baseline_markout_per_share": {"500": -0.05},
                    "overlay_markout_per_share": (
                        {"500": -0.05 + stress_improvement}
                        if stress_improvement < 0.0 else {}
                    ),
                }
            },
        }

    def run_rows(self, rows: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episodes.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            return forward.evaluate(REGISTRY, [path])

    def test_insufficient_forward_evidence_is_not_failure_or_pass(self) -> None:
        report = self.run_rows([self.episode(0, 0)])
        self.assertEqual(report["state"], "FORWARD_EVIDENCE_INSUFFICIENT")
        self.assertIn("INSUFFICIENT_INDEPENDENT_MARKETS", report["reason_codes"])
        self.assertIn("INSUFFICIENT_AVOIDABLE_FILL_EVENTS", report["reason_codes"])

    def test_complete_positive_forward_panel_passes_all_frozen_gates(self) -> None:
        rows = [self.episode(market, index) for market in range(30) for index in range(2)]
        report = self.run_rows(rows)
        self.assertEqual(report["state"], "PASS")
        self.assertEqual(report["market_count"], 30)
        self.assertEqual(report["avoidable_fill_events"], 60)
        self.assertGreater(report["equal_weight_500ms_improvement_per_share"], 0.0)
        self.assertGreater(report["leave_best_market_out_500ms_improvement_per_share"], 0.0)
        self.assertEqual(report["positive_market_fraction"], 1.0)
        self.assertGreater(report["stress_3x_queue_200ms_cancel_improvement_per_share"], 0.0)
        self.assertEqual(report["reason_codes"], [])

    def test_negative_stress_fails_after_minimum_evidence_is_met(self) -> None:
        rows = [
            self.episode(market, index, stress_improvement=-0.10)
            for market in range(30) for index in range(2)
        ]
        report = self.run_rows(rows)
        self.assertEqual(report["state"], "FAIL")
        self.assertIn("STRESS_3X_QUEUE_200MS_CANCEL_NOT_POSITIVE", report["reason_codes"])

    def test_prefreeze_and_model_lookahead_are_rejected(self) -> None:
        row = self.episode(0, 0)
        row["quote_receive_ms"] = self.freeze_ms
        with self.assertRaisesRegex(forward.EvidenceError, "not_strictly_forward"):
            self.run_rows([row])
        row = self.episode(0, 0)
        row["maker_model_published_ms"] = row["quote_receive_ms"]
        with self.assertRaisesRegex(forward.EvidenceError, "maker_model_lookahead"):
            self.run_rows([row])

    def test_rule_drift_and_overlay_created_fill_fail_closed(self) -> None:
        row = self.episode(0, 0)
        row["rule_sha256"] = "0" * 64
        with self.assertRaisesRegex(forward.EvidenceError, "rule_hash_drift"):
            self.run_rows([row])
        row = self.episode(0, 0)
        row["baseline_fill"] = False
        row["overlay_fill"] = True
        with self.assertRaisesRegex(forward.EvidenceError, "overlay_created_fill"):
            self.run_rows([row])

    def test_duplicate_episode_identity_is_rejected(self) -> None:
        row = self.episode(0, 0)
        with self.assertRaisesRegex(forward.EvidenceError, "duplicate_episode"):
            self.run_rows([row, dict(row)])


if __name__ == "__main__":
    unittest.main()
