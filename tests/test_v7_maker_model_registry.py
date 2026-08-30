#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_maker_model_registry import (
    RegistryError,
    _sha256,
    promote_challenger,
    register_challenger,
    rollback_champion,
)

SHA = "b" * 40


class MakerModelRegistryTests(unittest.TestCase):
    def challenger_model(self) -> dict:
        return {
            "schema": "polymarket_v7_maker_execution_model_v1",
            "strategy": "MICRO_MAKER_PRO",
            "family": "test-family",
            "version": 123,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": SHA,
            "code_sha": SHA,
            "policy_version": 1,
            "artifact_role": "challenger",
            "promotion_state": "CHALLENGER_PENDING_OOS",
            "eligible_for_live_reload": False,
            "model_state": "CHALLENGER",
            "feature_schema": {"version": 1},
            "training_window": {
                "start_ts_ms": 100,
                "end_ts_ms": 200,
                "records": 500,
                "orders": 100,
                "event_clusters": 15,
            },
            "hyperparameters": {"cold_fill_prior": 0.02},
            "groups": {"GLOBAL": {"fill_probability": 0.05}},
        }

    def write_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def validation(self, challenger_sha256: str) -> dict:
        return {
            "model_sha": SHA,
            "paper_only": True,
            "authenticated_execution": False,
            "challenger_sha256": challenger_sha256,
            "status": "PASS",
            "chronological_oos": True,
            "common_sample": True,
            "shadow_paper": True,
            "robust_ev_positive": True,
            "execution_healthy": True,
            "latency_healthy": True,
            "inventory_controlled": True,
            "queue_credible": True,
            "fill_calibrated": True,
            "markout_calibrated": True,
            "event_clusters": 15,
            "oos_policy_improvement": 0.001,
        }

    def test_registering_challenger_never_creates_or_overwrites_champion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            challenger = root / "challenger.json"
            champion = root / "champion.json"
            registry = root / "registry.json"
            self.write_json(challenger, self.challenger_model())

            state = register_challenger(
                challenger=challenger,
                registry_path=registry,
                model_sha=SHA,
                champion=champion,
            )
            self.assertFalse(champion.exists())
            self.assertEqual(state["challenger"]["status"], "PENDING_OOS")
            self.assertEqual(state["champion"]["status"], "STATIC_POLICY_BASELINE")
            self.assertEqual(state["history"][-1]["action"], "REGISTER_CHALLENGER")

    def test_promotion_requires_explicit_operator_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            challenger = root / "challenger.json"
            champion = root / "champion.json"
            registry = root / "registry.json"
            validation = root / "validation.json"
            self.write_json(challenger, self.challenger_model())
            register_challenger(
                challenger=challenger,
                registry_path=registry,
                model_sha=SHA,
                champion=champion,
            )
            self.write_json(validation, self.validation(_sha256(challenger)))

            with self.assertRaises(RegistryError):
                promote_challenger(
                    challenger=challenger,
                    champion=champion,
                    registry_path=registry,
                    validation_report=validation,
                    model_sha=SHA,
                    operator_approval="",
                )
            self.assertFalse(champion.exists())

    def test_promotion_rejects_non_oos_or_unhealthy_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            challenger = root / "challenger.json"
            champion = root / "champion.json"
            registry = root / "registry.json"
            validation = root / "validation.json"
            self.write_json(challenger, self.challenger_model())
            register_challenger(
                challenger=challenger,
                registry_path=registry,
                model_sha=SHA,
                champion=champion,
            )
            report = self.validation(_sha256(challenger))
            report["chronological_oos"] = False
            self.write_json(validation, report)

            with self.assertRaises(RegistryError):
                promote_challenger(
                    challenger=challenger,
                    champion=champion,
                    registry_path=registry,
                    validation_report=validation,
                    model_sha=SHA,
                    operator_approval="paper-model-review-1",
                )
            self.assertFalse(champion.exists())

    def test_validated_explicit_promotion_creates_live_reloadable_paper_champion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            challenger = root / "challenger.json"
            champion = root / "champion.json"
            registry = root / "registry.json"
            validation = root / "validation.json"
            self.write_json(challenger, self.challenger_model())
            register_challenger(
                challenger=challenger,
                registry_path=registry,
                model_sha=SHA,
                champion=champion,
            )
            self.write_json(validation, self.validation(_sha256(challenger)))

            state = promote_challenger(
                challenger=challenger,
                champion=champion,
                registry_path=registry,
                validation_report=validation,
                model_sha=SHA,
                operator_approval="paper-model-review-1",
            )
            promoted = json.loads(champion.read_text(encoding="utf-8"))
            self.assertEqual(promoted["artifact_role"], "champion")
            self.assertEqual(promoted["promotion_state"], "CHAMPION")
            self.assertTrue(promoted["eligible_for_live_reload"])
            self.assertTrue(promoted["paper_only"])
            self.assertFalse(promoted["authenticated_execution"])
            self.assertFalse(promoted["real_order_submission"])
            self.assertEqual(state["champion"]["status"], "CHAMPION")
            self.assertEqual(state["history"][-1]["action"], "PROMOTE_CHALLENGER")

    def test_promotion_preserves_previous_and_explicit_rollback_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            challenger = root / "challenger.json"
            champion = root / "champion.json"
            registry = root / "registry.json"
            validation = root / "validation.json"
            incumbent = self.challenger_model()
            incumbent.update({
                "artifact_role": "champion", "promotion_state": "CHAMPION",
                "eligible_for_live_reload": True, "version": 99,
            })
            self.write_json(champion, incumbent)
            self.write_json(challenger, self.challenger_model())
            register_challenger(
                challenger=challenger, registry_path=registry,
                model_sha=SHA, champion=champion,
            )
            self.write_json(validation, self.validation(_sha256(challenger)))
            promote_challenger(
                challenger=challenger, champion=champion, registry_path=registry,
                validation_report=validation, model_sha=SHA,
                operator_approval="paper-promote",
            )
            self.assertEqual(json.loads(champion.read_text())["version"], 123)
            previous = champion.with_suffix(".previous.json")
            self.assertEqual(json.loads(previous.read_text())["version"], 99)

            state = rollback_champion(
                champion=champion, registry_path=registry, model_sha=SHA,
                operator_approval="paper-rollback",
            )
            self.assertEqual(json.loads(champion.read_text())["version"], 99)
            self.assertEqual(state["history"][-1]["action"], "ROLLBACK_CHAMPION")


if __name__ == "__main__":
    unittest.main()
