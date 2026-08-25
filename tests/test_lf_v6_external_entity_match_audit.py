#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "lf_v6_external_entity_match_audit.py"


def load_audit():
    spec = importlib.util.spec_from_file_location("lf_v6_external_entity_match_audit", AUDIT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load audit")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExternalEntityMatchAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = load_audit().fixture_report()

    def test_incumbent_pair_scoring_has_no_asset_gate(self):
        self.assertTrue(self.report["findings"]["incumbent_pair_scoring_has_no_entity_rejection"])

    def test_cross_asset_match_is_rejected(self):
        fixture = self.report["cross_asset_fixture"]
        self.assertEqual(fixture["pm_asset"], "BTC")
        self.assertIsNone(fixture["kalshi_asset"])
        self.assertEqual(fixture["challenger_rejection"], "entity_mismatch")

    def test_same_asset_match_survives_entity_gate(self):
        fixture = self.report["same_asset_fixture"]
        self.assertEqual(fixture["pm_asset"], "BTC")
        self.assertEqual(fixture["kalshi_asset"], "BTC")
        self.assertNotEqual(fixture["challenger_rejection"], "entity_mismatch")

    def test_aggressive_thresholds_keep_hard_semantic_requirements(self):
        policy = self.report["aggressive_research_policy"]
        self.assertEqual(policy["polymarket_max_markets"], 700)
        self.assertEqual(policy["polymarket_min_liquidity"], 10.0)
        self.assertEqual(policy["kalshi_max_markets"], 5000)
        self.assertLess(policy["same_entity_min_match_score"], 0.68)
        self.assertIn("entity_or_asset_compatibility", policy["hard_requirements"])
        self.assertIn("critical_number_match", policy["hard_requirements"])


if __name__ == "__main__":
    unittest.main()
