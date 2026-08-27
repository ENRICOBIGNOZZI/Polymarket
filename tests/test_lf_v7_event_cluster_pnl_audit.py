#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "lf_v7_event_cluster_pnl_audit.py"


def load_audit():
    spec = importlib.util.spec_from_file_location("lf_v7_event_cluster_pnl_audit", AUDIT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {AUDIT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EventClusterPnLAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = load_audit()
        cls.report = cls.audit.summarize(ROOT)

    def test_same_day_block_statistics_despite_radically_different_event_diversification(self) -> None:
        one = self.report["one_event_repeated_across_20_days"]
        many = self.report["twenty_distinct_events_across_20_days"]
        self.assertEqual(one["distinct_event_clusters"], 1)
        self.assertEqual(many["distinct_event_clusters"], 20)
        for key in (
            "bootstrap_one_sided_pvalue",
            "fold_count",
            "positive_fold_fraction",
            "terminal_pnl_observations",
            "net_pnl",
        ):
            self.assertEqual(one[key], many[key], key)
            self.assertTrue(self.report["incumbent_statistics_identical"][key])

    def test_single_event_can_look_significant_under_current_day_bootstrap(self) -> None:
        one = self.report["one_event_repeated_across_20_days"]
        self.assertEqual(one["terminal_pnl_observations"], 20)
        self.assertEqual(one["net_pnl"], 20.0)
        self.assertEqual(one["bootstrap_one_sided_pvalue"], 0.0)
        self.assertEqual(one["fold_count"], 2)
        self.assertEqual(one["positive_fold_fraction"], 1.0)
        self.assertFalse(self.report["event_cluster_inference"]["one_event_uncertainty_estimable"])

    def test_audit_stays_fail_closed(self) -> None:
        self.assertEqual(self.report["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertIn("event-cluster", " ".join(self.report["required_successor"]))


if __name__ == "__main__":
    unittest.main()
