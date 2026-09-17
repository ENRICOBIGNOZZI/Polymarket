from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_lead_lag_signal_age_report import analyze


def fill(order: str, market: str, age: float) -> dict:
    return {
        "event_type": "FILL", "order_id": order, "market_id": market,
        "metadata": {"model_family": "lead_lag_taker_v1", "signal_age_ms_at_fill": age},
    }


def final(order: str, market: str, pnl: float) -> dict:
    return {
        "event_type": "FINAL", "order_id": order, "market_id": market,
        "final_pnl": pnl, "metadata": {"model_family": "lead_lag_taker_v1"},
    }


class LeadLagSignalAgeReportTests(unittest.TestCase):
    def test_thresholds_are_descriptive_and_never_promote(self) -> None:
        ledger = [fill("a", "m1", 50), final("a", "m1", -2),
                  fill("b", "m2", 1500), final("b", "m2", 1)]
        report = analyze(ledger, [])
        self.assertFalse(report["frozen_v1_modified"])
        self.assertFalse(report["threshold_selection_authorized"])
        self.assertFalse(report["automatic_promotion"])
        by_cap = {x["maximum_signal_age_ms"]: x for x in report["cumulative_thresholds"]}
        self.assertEqual(by_cap[100]["settled_markets"], 1)
        self.assertEqual(by_cap[100]["total_pnl"], -2)
        self.assertEqual(by_cap[2000]["settled_markets"], 2)
        self.assertEqual(by_cap[2000]["total_pnl"], -1)

    def test_event_age_fallback_preserves_missingness(self) -> None:
        ledger = [final("a", "m1", 1), final("b", "m2", -1)]
        events = [{"event": "FILLED", "market_id": "m1", "signal_age_ms": 80}]
        report = analyze(ledger, events)
        self.assertEqual(report["settled_with_observed_signal_age"], 1)
        self.assertEqual(report["settled_missing_signal_age"], 1)
        row = {x["market_id"]: x for x in report["rows"]}
        self.assertEqual(row["m1"]["signal_age_ms"], 80)
        self.assertIsNone(row["m2"]["signal_age_ms"])

    def test_conflicting_partial_fill_ages_fail_closed(self) -> None:
        ledger = [fill("a", "m1", 50), fill("a", "m1", 90), final("a", "m1", 2)]
        report = analyze(ledger, [])
        self.assertEqual(report["conflicting_fill_age_orders"], ["a"])
        self.assertEqual(report["settled_missing_signal_age"], 1)
        self.assertIsNone(report["rows"][0]["signal_age_ms"])

    def test_other_strategies_are_excluded(self) -> None:
        other = final("x", "m9", 100)
        other["metadata"]["model_family"] = "professional_maker"
        report = analyze([other, fill("a", "m1", 200), final("a", "m1", 1)], [])
        self.assertEqual(report["settled"]["settled_markets"], 1)
        self.assertEqual(report["settled"]["total_pnl"], 1)

    def test_disjoint_buckets_do_not_double_count(self) -> None:
        ledger = [fill("a", "m1", 100), final("a", "m1", 1),
                  fill("b", "m2", 101), final("b", "m2", 2),
                  fill("c", "m3", 5000), final("c", "m3", -1)]
        report = analyze(ledger, [])
        counts = sum(x["settled_markets"] for x in report["disjoint_age_buckets"])
        self.assertEqual(counts, 3)
        self.assertEqual(report["settled"]["total_pnl"], 2)


if __name__ == "__main__":
    unittest.main()
