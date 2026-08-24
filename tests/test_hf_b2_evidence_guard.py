#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hf_b2_evidence_guard import assess_probe


class HFB2EvidenceGuardTest(unittest.TestCase):
    @staticmethod
    def payload(snapshot_count: int = 13, duration: int = 120) -> dict:
        rows = []
        for latency in (250, 500, 750):
            rows.append(
                {
                    "candidate_market": "b2",
                    "policy": "join",
                    "arrival_latency_ms": latency,
                    "queue_multiplier": 1.0,
                    "full_completion": False,
                    "filled_legs": 0,
                    "bundle_completion_fraction": 0.0,
                    "force_completion_cost_usd": 1.25,
                    "filled_markout_60_usd": 0.0,
                    "filled_markout_300_usd": 0.0,
                    "initial_weighted_imbalance_l1": 0.1,
                    "initial_weighted_microprice_minus_mid": 0.002,
                    "snapshot_ofi_per_second": 3.0,
                }
            )
        return {
            "quote_start_ts": 1000,
            "quote_end_ts": 1000 + duration,
            "book_snapshots": snapshot_count,
            "method": {"arrival_latencies_ms": [250, 500, 750]},
            "results": rows,
        }

    def test_subsecond_latency_is_not_independent_evidence_with_coarse_replay(self) -> None:
        report = assess_probe(self.payload(), trade_timestamp_resolution_ms=1000.0)
        latency = report["latency"]
        self.assertFalse(latency["trade_cutoff_resolved"])
        self.assertFalse(latency["arrival_book_resolved"])
        self.assertFalse(latency["latency_scenarios_count_as_independent_evidence"])
        self.assertEqual(latency["multi_latency_comparison_groups"], 1)
        self.assertEqual(latency["groups_with_distinct_observed_outcomes"], 0)

    def test_post_decision_ofi_is_not_predictor_safe(self) -> None:
        report = assess_probe(self.payload())
        causality = report["causality"]
        self.assertIn("snapshot_ofi_per_second", causality["post_decision_diagnostics_not_predictor_safe"])
        self.assertNotIn("snapshot_ofi_per_second", causality["predictor_safe_at_quote_start"])

    def test_fine_event_time_can_identify_latency_scenarios(self) -> None:
        report = assess_probe(
            self.payload(snapshot_count=2401, duration=120),
            trade_timestamp_resolution_ms=50.0,
        )
        latency = report["latency"]
        self.assertTrue(latency["trade_cutoff_resolved"])
        self.assertTrue(latency["arrival_book_resolved"])
        self.assertTrue(latency["latency_scenarios_count_as_independent_evidence"])


if __name__ == "__main__":
    unittest.main()
