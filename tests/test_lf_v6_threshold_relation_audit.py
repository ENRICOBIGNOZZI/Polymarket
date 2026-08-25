#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "lf_v6_threshold_relation_audit.py"


def load_audit():
    spec = importlib.util.spec_from_file_location("lf_v6_threshold_relation_audit", AUDIT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load audit")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class V6TypedThresholdRelationAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = load_audit()
        cls.report = cls.audit.fixture_report()

    def test_incumbent_merges_different_expiry_days(self):
        self.assertTrue(self.report["findings"]["incumbent_merges_different_day_expiries"])
        self.assertFalse(self.report["findings"]["typed_challenger_merges_different_day_expiries"])

    def test_cross_expiry_basket_has_no_one_dollar_payoff_floor(self):
        btc_aug25 = 90_000.0
        btc_aug31 = 160_000.0
        yes_low_early = 1.0 if btc_aug25 > 100_000.0 else 0.0
        no_high_late = 1.0 if btc_aug31 <= 150_000.0 else 0.0
        self.assertEqual(yes_low_early + no_high_late, 0.0)

    def test_incumbent_count_threshold_can_be_replaced_by_year(self):
        self.assertTrue(self.report["findings"]["incumbent_count_thresholds_choose_year"])
        self.assertTrue(self.report["findings"]["typed_count_thresholds_are_distinct"])

    def test_or_more_is_recovered_without_relaxing_economic_gate(self):
        self.assertTrue(self.report["findings"]["incumbent_or_more_is_unrecognized"])
        self.assertTrue(self.report["findings"]["typed_or_more_is_recognized"])

    def test_suffix_does_not_consume_the_b_in_by(self):
        self.assertTrue(self.report["findings"]["incumbent_suffix_bleeds_into_by"])
        self.assertTrue(self.report["findings"]["typed_money_by_is_100k"])

    def test_expiry_is_part_of_typed_family_identity(self):
        a = self.audit.typed_threshold_signature("Will the price of Bitcoin be above $100,000 on August 25, 2026?")
        b = self.audit.typed_threshold_signature("Will the price of Bitcoin be above $150,000 on August 31, 2026?")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertNotEqual(a.expiry, b.expiry)
        self.assertNotEqual(a.family, b.family)

    def test_same_expiry_money_ladder_remains_comparable(self):
        a = self.audit.typed_threshold_signature("Will the price of Bitcoin be above $100,000 on August 31, 2026?")
        b = self.audit.typed_threshold_signature("Will the price of Bitcoin be above $150,000 on August 31, 2026?")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a.family, b.family)
        self.assertEqual((a.threshold, b.threshold), (100000.0, 150000.0))

    def test_percent_threshold_keeps_type(self):
        sig = self.audit.typed_threshold_signature("Will Futuro Nazionale get at least 3% of the vote in the next Italian general elections?")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.kind, "percent")
        self.assertAlmostEqual(sig.threshold, 0.03)
        self.assertTrue(self.report["findings"]["typed_percent_is_preserved"])


if __name__ == "__main__":
    unittest.main()
