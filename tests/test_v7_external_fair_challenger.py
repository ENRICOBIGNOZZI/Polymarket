import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_fair_challenger import freeze_challenger  # noqa: E402

SHA = "a" * 40
RULES = "b" * 64


class ExternalFairChallengerTests(unittest.TestCase):
    def test_freezes_once_and_reserves_future_contracts_for_forward_oos(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            config = root / "external.json"
            config_value = {"paper_only": True, "execution_authority": "SHADOW_ZERO_AUTHORITY"}
            config.write_text(json.dumps(config_value), encoding="utf-8")
            policy_hash = hashlib.sha256(json.dumps(
                config_value, separators=(",", ":"), sort_keys=True
            ).encode()).hexdigest()
            tape = root / "counterfactuals.jsonl"
            rows = []
            for index in range(20):
                forecast_id = f"forecast-{index}"
                common = {
                    "schema": "polymarket_v7_external_fair_counterfactual_v1",
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "execution_authority": "SHADOW_ZERO_AUTHORITY",
                    "model_version": "external-fair-structural-v7-paper",
                    "policy_sha256": policy_hash,
                    "evidence_semantics_version": "external-fair-settlement-evidence-v1",
                    "forecast_id": forecast_id,
                    "market_id": f"market-{index}",
                }
                rows.append({
                    **common, "record_id": f"origin-{index}", "event_type": "FORECAST",
                    "rules_hash": RULES, "external_only_yes": 0.35 + 0.015 * index,
                })
                rows.append({
                    **common, "record_id": f"final-{index}",
                    "event_type": "FORECAST_FINAL", "timestamp_ms": 1_000 + index,
                    "external_only_yes": 0.35 + 0.015 * index,
                    "actual_yes": float(index % 2),
                })
            tape.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            registry = root / "registry"
            status = root / "status.json"

            first = freeze_challenger(
                tape_paths=[tape], registry_root=registry, config_path=config,
                model_sha=SHA, status_path=status, minimum_contracts=20,
            )
            self.assertEqual(first["state"], "FROZEN_CHALLENGER_PUBLISHED")
            self.assertEqual(first["independent_settlement_markets"], 20)
            pointer = json.loads((registry / "fair_value_challenger.json").read_text())
            artifact = json.loads(pathlib.Path(pointer["artifact"]).read_text())
            self.assertEqual(artifact["code_sha"], SHA)
            self.assertEqual(artifact["training_contracts"], 20)
            self.assertEqual(
                artifact["oos_scores"]["state"],
                "AWAITING_IMMUTABLE_FORWARD_SETTLEMENTS",
            )

            second = freeze_challenger(
                tape_paths=[tape], registry_root=registry, config_path=config,
                model_sha=SHA, status_path=status, minimum_contracts=20,
            )
            self.assertEqual(second["state"], "FROZEN_CHALLENGER_REUSED")
            self.assertEqual(second["model_hash"], first["model_hash"])


if __name__ == "__main__":
    unittest.main()
