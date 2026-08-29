#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("attach_external_evidence", ROOT / "scripts" / "attach_external_evidence.py")
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)


class AttachExternalEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_800_000_000
        self.alpha = {
            "schema": "polymarket_alpha_factory_report_v1", "status": "RESEARCHING",
            "recommended_canary": None, "champion": {"version": 4}, "diagnostics": {},
            "candidates": [], "next_experiments": [], "paper_only": True,
            "submitted_orders": 0, "authenticated_execution": False, "direct_champion_mutation": False,
        }
        self.state = {
            "active_canary": None, "recommended_canary": None, "candidates": {},
            "invariants": {"direct_champion_mutation": False, "authenticated_execution": False},
        }
        self.external = {
            "schema": "polymarket_external_intelligence_report_v1",
            "generated_ts": self.now - 60, "status": "VALIDATED_CHALLENGER_EVIDENCE",
            "paper_only": True, "submitted_orders": 0, "authenticated_execution": False,
            "direct_champion_mutation": False, "production_signal_write": False,
            "collection": {"new_observations": 100, "accepted_signal_rows": 10},
            "backtest": {"candidate_count": 3, "passing_candidates": 1},
            "source_reliability": {"kalshi": {"score": 0.7}},
            "source_health": {"kalshi": {"status": "ok"}},
            "alpha_factory_evidence": {
                "candidate_id": "external:kalshi:external_probability:21600s",
                "family": "external_information", "specification": "kalshi:external_probability:21600s",
                "evidence_type": "purged_chronological_external_information_backtest",
                "observations": 80, "raw_pvalue": 0.02, "gate_pass_before_fdr": True,
                "integration_evidence_pass": False,
                "integration_reasons": ["exact_executable_clob_replay_and_incumbent_ablation_required"],
                "reasons": [], "critical_failures": [], "metrics": {"incremental_utility": 0.04},
            },
        }

    def test_attaches_shadow_candidate_without_promotion(self) -> None:
        report, state = adapter.attach(self.alpha, self.state, self.external,
                                       now=self.now, max_age_seconds=10800)
        self.assertTrue(report["diagnostics"]["external_intelligence"]["fresh"])
        self.assertEqual(len(report["candidates"]), 1)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["decision"], "continue_shadow")
        self.assertFalse(candidate["integration_evidence_pass"])
        self.assertIn("external_evidence_requires_alpha_factory_replay", candidate["reasons"])
        self.assertIsNone(report["recommended_canary"])
        self.assertIsNone(state["recommended_canary"])
        self.assertIsNone(state["active_canary"])
        self.assertFalse(report["direct_champion_mutation"])
        self.assertFalse(report["authenticated_execution"])
        self.assertEqual(report["submitted_orders"], 0)
        self.assertEqual(report["next_experiments"][0]["experiment_id"],
                         "external_exact_clob_replay_and_incumbent_ablation")

    def test_stale_report_is_diagnostic_only(self) -> None:
        stale = json.loads(json.dumps(self.external))
        stale["generated_ts"] = self.now - 20000
        report, state = adapter.attach(self.alpha, self.state, stale,
                                       now=self.now, max_age_seconds=10800)
        self.assertFalse(report["diagnostics"]["external_intelligence"]["fresh"])
        self.assertEqual(report["candidates"], [])
        self.assertNotIn("external_evidence_candidate", state)

    def test_rejects_execution_claim(self) -> None:
        unsafe = json.loads(json.dumps(self.external))
        unsafe["submitted_orders"] = 1
        with self.assertRaises(ValueError):
            adapter.attach(self.alpha, self.state, unsafe, now=self.now, max_age_seconds=10800)


if __name__ == "__main__":
    unittest.main()
