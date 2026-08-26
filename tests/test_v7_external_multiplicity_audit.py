#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_multiplicity_audit import audit_report, step_up_rejections


class ExternalMultiplicityAuditTest(unittest.TestCase):
    def make_report(self, pvalues, gate_indices=(0,)):
        candidates = []
        for index, pvalue in enumerate(pvalues):
            candidates.append({
                "candidate_id": f"candidate-{index}",
                "raw_pvalue": pvalue,
                "gate_pass": index in gate_indices,
            })
        return {
            "schema": "polymarket_external_intelligence_report_v1",
            "generated_ts": 1787786186,
            "backtest": {
                "candidate_count": len(candidates),
                "candidates": candidates,
            },
            "alpha_factory_evidence": {"candidate_id": "candidate-0"},
        }

    def test_current_shape_fails_bh_and_by(self):
        report = self.make_report([0.004995004995004995, 0.28771228771228774] + [1.0] * 19)
        result = audit_report(report, 0.10)
        selected = result["selected_candidate"]
        self.assertEqual(result["family_size"], 21)
        self.assertAlmostEqual(result["bh_rank1_threshold"], 0.1 / 21)
        self.assertFalse(selected["bh_q_rejected"])
        self.assertFalse(selected["by_q_rejected"])
        self.assertAlmostEqual(selected["bonferroni_adjusted_pvalue"], 0.1048951048951049)
        self.assertEqual(result["bh_rejection_count"], 0)
        self.assertEqual(result["by_rejection_count"], 0)
        self.assertEqual(
            result["state"],
            "STRONG_RAW_LEAD_MULTIPLICITY_BLOCKED_MORE_EVIDENCE_REQUIRED",
        )
        self.assertFalse(result["promotion_allowed"])
        self.assertFalse(result["q_external_materialized"])

    def test_bh_step_up_can_reject_multiple_predeclared_tests(self):
        flags = step_up_rejections([0.003, 0.008, 0.7, 0.9], 0.10, dependence_robust=False)
        self.assertEqual(flags, [True, True, False, False])

    def test_dependence_robust_by_is_stricter(self):
        bh = step_up_rejections([0.01, 0.02, 0.8, 0.9], 0.10, dependence_robust=False)
        by = step_up_rejections([0.01, 0.02, 0.8, 0.9], 0.10, dependence_robust=True)
        self.assertEqual(bh, [True, True, False, False])
        self.assertEqual(by, [False, False, False, False])

    def test_partial_candidate_family_fails_closed(self):
        report = self.make_report([0.01, 0.2])
        report["backtest"]["candidate_count"] = 3
        with self.assertRaisesRegex(ValueError, "partial candidate family"):
            audit_report(report)

    def test_invalid_pvalue_fails_closed(self):
        report = self.make_report([0.01, float("nan")])
        with self.assertRaisesRegex(ValueError, "invalid p-value|outside"):
            audit_report(report)

    def test_selected_candidate_must_belong_to_family(self):
        report = self.make_report([0.01, 0.2])
        report["alpha_factory_evidence"]["candidate_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "selected candidate"):
            audit_report(report)


if __name__ == "__main__":
    unittest.main()
