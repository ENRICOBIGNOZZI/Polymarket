#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v7_ranking_survivorship_gate_audit.py"
SPEC = importlib.util.spec_from_file_location("lf_v7_ranking_survivorship_gate_audit", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RankingSurvivorshipGateAuditTests(unittest.TestCase):
    def test_current_registered_workflow_accepts_known_survivorship_unsafe_report(self):
        workflow = (ROOT / ".github/workflows/v7-cross-sectional-ranking-research.yml").read_text()
        result = MODULE.audit_workflow_text(workflow)
        self.assertTrue(result.workflow_has_unsafe_survivorship_assertion)
        self.assertFalse(result.workflow_requires_survivorship_safe_true)
        self.assertTrue(result.workflow_publishes_ranking_metrics)
        self.assertFalse(result.promotion_evidence_contract_valid)
        self.assertEqual(result.decision, "BLOCKING_SURVIVORSHIP_EVIDENCE_CONTRACT")

    def test_corrected_contract_requires_survivorship_safe_true(self):
        workflow = '''
        assert report["survivorship_safe"] is True
        print(h["mean_daily_rank_ic"])
        '''
        result = MODULE.audit_workflow_text(workflow)
        self.assertFalse(result.workflow_has_unsafe_survivorship_assertion)
        self.assertTrue(result.workflow_requires_survivorship_safe_true)
        self.assertTrue(result.workflow_publishes_ranking_metrics)
        self.assertTrue(result.promotion_evidence_contract_valid)
        self.assertEqual(result.decision, "PASS")

    def test_missing_gate_fails_closed(self):
        result = MODULE.audit_workflow_text('print("mean_daily_rank_ic")')
        self.assertFalse(result.promotion_evidence_contract_valid)
        self.assertEqual(result.decision, "BLOCKING_SURVIVORSHIP_EVIDENCE_CONTRACT")


if __name__ == "__main__":
    unittest.main()
