from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v7_model_evidence_horizon_audit.py"
spec = importlib.util.spec_from_file_location("lf_v7_model_evidence_horizon_audit_test", SCRIPT)
assert spec and spec.loader
audit_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit_module
spec.loader.exec_module(audit_module)


class V7ModelEvidenceHorizonAuditTest(unittest.TestCase):
    def test_required_lf_pca_ranking_surfaces_are_absent_from_current_config(self) -> None:
        report = audit_module.audit()
        required = set(audit_module.REQUIRED_MODEL_SURFACES)
        self.assertEqual(set(report["missing_from_config"]), required)
        self.assertEqual(
            set(report["configured_model_names"]),
            {"external", "graph_hard", "micro_maker", "micro_taker", "relative_value"},
        )

    def test_normalizer_silently_drops_new_v7_model_horizon_contracts(self) -> None:
        report = audit_module.audit()
        required = set(audit_module.REQUIRED_MODEL_SURFACES)
        self.assertTrue(report["silent_drop_demonstrated"])
        self.assertEqual(set(report["dropped_after_normalization"]), required)
        self.assertTrue(required.isdisjoint(report["normalized_default_model_names"]))

    def test_required_lanes_have_no_execution_source_mapping(self) -> None:
        report = audit_module.audit()
        self.assertEqual(
            set(report["unmapped_strategy_paths"]),
            set(audit_module.REQUIRED_MODEL_SURFACES),
        )

    def test_horizons_remain_explicit_and_unpooled_in_required_contract(self) -> None:
        required = audit_module.REQUIRED_MODEL_SURFACES
        self.assertEqual(required["pca_30m"]["horizon_seconds"], 1800)
        self.assertEqual(required["pca_1h"]["horizon_seconds"], 3600)
        self.assertEqual(required["pca_2h"]["horizon_seconds"], 7200)
        self.assertEqual(required["pca_6h"]["horizon_seconds"], 21600)
        self.assertEqual(required["ranking_2h"]["horizon_seconds"], 7200)
        self.assertEqual(required["ranking_6h"]["horizon_seconds"], 21600)
        self.assertEqual(
            required["local_factor"]["horizon_semantics"],
            "candidate_actual_hold_horizon_required",
        )

    def test_audit_is_fail_closed_and_never_claims_alpha(self) -> None:
        report = audit_module.audit()
        self.assertTrue(report["material"])
        self.assertEqual(report["state"], "MODEL_HORIZON_EXECUTION_EVIDENCE_BLOCKER")
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertTrue(report["paper_only"])
        self.assertFalse(report["authenticated_execution"])


if __name__ == "__main__":
    unittest.main()
