#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("alpha_factory", ROOT / "scripts" / "alpha_factory.py")
assert SPEC and SPEC.loader
alpha_factory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alpha_factory)


class AlphaFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config" / "alpha_factory.json").read_text(encoding="utf-8"))
        self.champion = {
            "schema_version": 1,
            "version": 4,
            "loop": "scripts/paper_v4_loop.sh",
            "config": "config/paper_v4.json",
            "run_root": "runs/paper_v4_live",
            "deployment_ref": "paper-validated",
            "promotion_policy": "approved integration PR only",
        }
        self.now = 1_800_000_000

    def passing_live(self) -> dict:
        return {
            "schema": "polymarket_public_live_smoke_v2",
            "generated_ts": self.now - 60,
            "candidates": {"b1": [], "b2": [], "b3_rewards": []},
            "walk_forward": {
                "input_trades": 60,
                "active_folds": 4,
                "positive_active_folds": 3,
                "bootstrap_one_sided_pvalue": 0.001,
                "production_threshold": 0.003,
                "incremental_utility": 0.02,
                "single_model_compatible": True,
                "eligible_for_tiny_pilot": True,
                "gate_failures": [],
                "oos": {
                    "trades": 50,
                    "gross_pnl": 100.0,
                    "fees": 10.0,
                    "slippage": 10.0,
                    "net_pnl": 80.0,
                    "max_drawdown": 0.05,
                    "profit_factor": 2.0,
                },
                "oos_cost_stress": {
                    "trades": 50,
                    "net_pnl": 70.0,
                },
            },
        }

    def failing_live(self) -> dict:
        payload = self.passing_live()
        payload["walk_forward"]["oos"].update(
            {
                "gross_pnl": -10.0,
                "fees": 10.0,
                "slippage": 10.0,
                "net_pnl": -30.0,
                "max_drawdown": 0.15,
                "profit_factor": 0.5,
            }
        )
        payload["walk_forward"]["oos_cost_stress"]["net_pnl"] = -40.0
        payload["walk_forward"]["bootstrap_one_sided_pvalue"] = 0.8
        payload["walk_forward"]["positive_active_folds"] = 1
        return payload

    def test_benjamini_hochberg(self) -> None:
        result = alpha_factory.benjamini_hochberg(
            {"a": 0.001, "b": 0.02, "c": 0.50}, 0.05
        )
        self.assertTrue(result["a"]["rejected"])
        self.assertTrue(result["b"]["rejected"])
        self.assertFalse(result["c"]["rejected"])
        self.assertLessEqual(result["a"]["adjusted_pvalue"], result["b"]["adjusted_pvalue"])

    def test_config_rejects_execution_or_direct_mutation(self) -> None:
        bad = json.loads(json.dumps(self.config))
        bad["allow_authenticated_execution"] = True
        with self.assertRaises(ValueError):
            alpha_factory.validate_config(bad)
        bad = json.loads(json.dumps(self.config))
        bad["allow_direct_champion_mutation"] = True
        with self.assertRaises(ValueError):
            alpha_factory.validate_config(bad)

    def test_missing_evidence_fails_closed_without_promotion(self) -> None:
        report, state = alpha_factory.build_report(
            self.config,
            self.champion,
            {},
            {},
            [],
            {},
            self.now,
        )
        self.assertEqual(report["status"], "DEGRADED_STALE_EVIDENCE")
        self.assertIsNone(report["recommended_canary"])
        self.assertTrue(report["paper_only"])
        self.assertFalse(report["authenticated_execution"])
        self.assertFalse(report["direct_champion_mutation"])
        self.assertFalse(state["invariants"]["authenticated_execution"])

    def test_candidate_needs_repeated_passes_then_becomes_integration_ready(self) -> None:
        identifier = "portfolio:unified_bundle_engine"
        previous = {
            "schema": alpha_factory.STATE_SCHEMA,
            "active_canary": None,
            "candidates": {
                identifier: {
                    "candidate_id": identifier,
                    "first_seen_ts": self.now - 7200,
                    "consecutive_passes": 2,
                }
            },
        }
        report, state = alpha_factory.build_report(
            self.config,
            self.champion,
            self.passing_live(),
            {},
            [],
            previous,
            self.now,
        )
        candidate = next(x for x in report["candidates"] if x["candidate_id"] == identifier)
        self.assertEqual(candidate["decision"], "integration_ready")
        self.assertEqual(candidate["consecutive_passes"], 3)
        self.assertEqual(report["recommended_canary"], identifier)
        self.assertEqual(report["status"], "INTEGRATION_RECOMMENDED")
        self.assertEqual(state["recommended_canary"], identifier)
        self.assertIsNone(state["active_canary"])

    def test_fdr_blocks_an_apparent_gate_pass(self) -> None:
        live = self.passing_live()
        live["walk_forward"]["bootstrap_one_sided_pvalue"] = 0.20
        previous = {
            "candidates": {
                "portfolio:unified_bundle_engine": {"consecutive_passes": 10}
            }
        }
        report, _ = alpha_factory.build_report(
            self.config, self.champion, live, {}, [], previous, self.now
        )
        candidate = next(
            x for x in report["candidates"]
            if x["candidate_id"] == "portfolio:unified_bundle_engine"
        )
        self.assertEqual(candidate["decision"], "continue_shadow")
        self.assertIn("fdr_gate", candidate["reasons"])
        self.assertIsNone(report["recommended_canary"])

    def test_canary_regression_recommends_rollback_but_does_not_mutate_champion(self) -> None:
        identifier = "portfolio:unified_bundle_engine"
        previous = {
            "active_canary": identifier,
            "champion": self.champion,
            "candidates": {identifier: {"consecutive_passes": 3}},
        }
        report, state = alpha_factory.build_report(
            self.config,
            self.champion,
            self.failing_live(),
            {},
            [],
            previous,
            self.now,
        )
        self.assertEqual(report["status"], "ROLLBACK_RECOMMENDED")
        self.assertTrue(report["rollback"]["recommended"])
        self.assertEqual(state["active_canary"], identifier)
        self.assertEqual(report["champion"], self.champion)
        self.assertFalse(report["promotion_contract"]["real_money_automation"])

    def test_cli_outputs_are_atomic_and_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            champion = root / "champion.json"
            live = root / "live.json"
            forward = root / "forward.json"
            history = root / "history.jsonl"
            state_in = root / "state-in.json"
            config.write_text(json.dumps(self.config), encoding="utf-8")
            champion.write_text(json.dumps(self.champion), encoding="utf-8")
            live.write_text(json.dumps(self.passing_live()), encoding="utf-8")
            forward.write_text("{}", encoding="utf-8")
            history.write_text("", encoding="utf-8")
            state_in.write_text("{}", encoding="utf-8")

            report, state = alpha_factory.build_report(
                self.config, self.champion, self.passing_live(), {}, [], {}, self.now
            )
            alpha_factory.atomic_json(root / "report.json", report)
            alpha_factory.atomic_json(root / "state.json", state)
            alpha_factory.atomic_write(root / "report.md", alpha_factory.render_markdown(report))
            self.assertEqual(json.loads((root / "report.json").read_text())["schema"], alpha_factory.SCHEMA)
            self.assertEqual(json.loads((root / "state.json").read_text())["schema"], alpha_factory.STATE_SCHEMA)
            self.assertIn("Polymarket Alpha Factory", (root / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
