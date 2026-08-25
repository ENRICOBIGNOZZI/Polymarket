from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v6_external_bridge", ROOT / "scripts" / "v6_external_bridge.py")
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class V6ExternalBridgeAuthorizationTest(unittest.TestCase):
    @staticmethod
    def report(*, integration_pass: bool, approved_id: str = "direct-1", candidate_id: str = "direct-1") -> dict:
        return {
            "alpha_factory_evidence": {
                "candidate_id": approved_id,
                "integration_evidence_pass": integration_pass,
            },
            "backtest": {
                "candidates": [
                    {
                        "candidate_id": candidate_id,
                        "source": "approved_model",
                        "feature_name": "external_probability",
                        "gate_pass": True,
                    },
                    {
                        "candidate_id": "raw-feature",
                        "source": "binance",
                        "feature_name": "return_1h",
                        "gate_pass": True,
                    },
                ]
            },
        }

    def test_candidate_gate_without_integration_evidence_cannot_materialize(self) -> None:
        approved = bridge.approved_direct_models(self.report(integration_pass=False))
        self.assertEqual(approved, set())

    def test_integration_evidence_must_match_exact_direct_candidate(self) -> None:
        approved = bridge.approved_direct_models(self.report(integration_pass=True))
        self.assertEqual(approved, {("approved_model", "external_probability")})
        mismatched = bridge.approved_direct_models(
            self.report(integration_pass=True, approved_id="different-candidate")
        )
        self.assertEqual(mismatched, set())

    def test_raw_features_never_become_terminal_probabilities(self) -> None:
        report = self.report(integration_pass=True)
        report["backtest"]["candidates"] = [
            {
                "candidate_id": "raw-feature",
                "source": "binance",
                "feature_name": "return_1h",
                "gate_pass": True,
            }
        ]
        report["alpha_factory_evidence"]["candidate_id"] = "raw-feature"
        self.assertEqual(bridge.approved_direct_models(report), set())

    def test_unvalidated_override_is_diagnostic_only_and_still_direct_only(self) -> None:
        approved = bridge.approved_direct_models(
            self.report(integration_pass=False), allow_unvalidated=True
        )
        self.assertEqual(approved, {("approved_model", "external_probability")})


if __name__ == "__main__":
    unittest.main()
