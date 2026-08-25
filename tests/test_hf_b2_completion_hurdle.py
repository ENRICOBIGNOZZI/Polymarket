#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from hf_b2_completion_hurdle import (
    analyze,
    break_even_completion_probability,
    broker_semantics,
    expected_edge_at_completion,
    iid_per_leg_fill_floor,
)


class HFB2CompletionHurdleTest(unittest.TestCase):
    def test_original_eight_leg_binary_hurdle_remains_a_stress_diagnostic(self) -> None:
        maker = 0.0029233
        taker = -0.0191312
        break_even = break_even_completion_probability(maker, taker)
        self.assertIsNotNone(break_even)
        self.assertAlmostEqual(float(break_even), 0.8674510871, places=9)
        per_leg = iid_per_leg_fill_floor(break_even, 8)
        self.assertIsNotNone(per_leg)
        self.assertAlmostEqual(float(per_leg), 0.9823825159, places=9)
        self.assertLess(expected_edge_at_completion(0.80, maker, taker), 0.0)
        self.assertGreater(expected_edge_at_completion(0.90, maker, taker), 0.0)

    def test_current_two_maker_positive_candidates_have_negative_binary_value_at_runtime_threshold(self) -> None:
        payload = {
            "git_sha": "2771249f2b23ada3e7c0be044876686e4d7eb6e7",
            "generated_ts": 1787631733,
            "b2_coherence": {
                "top_raw": [
                    {
                        "market": "2774057",
                        "slug": "strait-of-hormuz-traffic-returns-to-normal-by-september-30-20260702154339440",
                        "legs": "2774057:NO:1|3501950:YES:0.0554494|2034729:YES:0.921788|3215074:NO:0.31782|2176270:YES:0.242498|2774056:YES:3.32159|2737255:YES:0.246966",
                        "maker_entry_net_edge": "0.00222302",
                        "taker_net_edge": "-0.0132019",
                        "raw_expected_edge": "0.00302568",
                    },
                    {
                        "market": "608565",
                        "slug": "will-lionel-messi-win-the-2026-ballon-dor",
                        "legs": "608565:NO:1|608543:NO:2.91863|608541:YES:2.06712|608547:NO:0.132367",
                        "maker_entry_net_edge": "0.000480047",
                        "taker_net_edge": "-0.00268639",
                        "raw_expected_edge": "0.00181837",
                    },
                ]
            },
        }
        broker_source = (ROOT / "src" / "multileg_paper.cpp").read_text(encoding="utf-8")
        runtime_source = (ROOT / "scripts" / "paper_v5_loop.sh").read_text(encoding="utf-8")
        result = analyze(payload, broker_source=broker_source, runtime_loop_source=runtime_source)
        self.assertEqual(result["maker_positive_candidates"], 2)

        first, second = result["rows"]
        self.assertEqual(first["market"], "2774057")
        self.assertEqual(first["legs"], 7)
        self.assertAlmostEqual(
            first["hypothetical_binary_taker_fallback_break_even_probability"],
            0.8558812623,
            places=9,
        )
        self.assertAlmostEqual(
            first["iid_per_leg_fill_floor_for_hypothetical_break_even"],
            0.9780133621,
            places=9,
        )
        self.assertLess(first["binary_scenario_expected_edge_if_completion_75pct"], 0.0)

        self.assertEqual(second["market"], "608565")
        self.assertEqual(second["legs"], 4)
        self.assertAlmostEqual(
            second["hypothetical_binary_taker_fallback_break_even_probability"],
            0.8483952152,
            places=9,
        )
        self.assertAlmostEqual(
            second["iid_per_leg_fill_floor_for_hypothetical_break_even"],
            0.9597310653,
            places=9,
        )
        self.assertLess(second["binary_scenario_expected_edge_if_completion_75pct"], 0.0)

    def test_runtime_broker_contract_is_partial_completion_then_unwind_not_forced_taker_completion(self) -> None:
        broker_source = (ROOT / "src" / "multileg_paper.cpp").read_text(encoding="utf-8")
        runtime_source = (ROOT / "scripts" / "paper_v5_loop.sh").read_text(encoding="utf-8")
        contract = broker_semantics(broker_source, runtime_source)
        self.assertAlmostEqual(contract["runtime_completion_threshold_min_leg_fraction"], 0.75)
        self.assertAlmostEqual(contract["runtime_submit_latency_ms"], 100.0)
        self.assertAlmostEqual(contract["runtime_cancel_latency_ms"], 100.0)
        self.assertAlmostEqual(contract["runtime_max_unmatched_leg_risk_usd"], 12.0)
        self.assertTrue(contract["completion_is_minimum_leg_fill_fraction"])
        self.assertTrue(contract["timeout_transitions_to_abort"])
        self.assertTrue(contract["abort_unwinds_only_filled_inventory"])
        self.assertFalse(contract["buys_missing_legs_as_taker_on_timeout"])
        self.assertTrue(contract["unmatched_leg_risk_gate_remains_active_for_complete_state"])
        self.assertFalse(contract["binary_taker_fallback_matches_live_runtime"])

    def test_non_maker_positive_rows_are_excluded(self) -> None:
        payload = {
            "git_sha": "abc",
            "generated_ts": 1,
            "b2_coherence": {
                "top_raw": [
                    {
                        "market": "positive",
                        "legs": "1:YES:1|2:NO:1",
                        "maker_entry_net_edge": "0.01",
                        "taker_net_edge": "-0.03",
                        "raw_expected_edge": "0.02",
                    },
                    {
                        "market": "negative",
                        "legs": "3:YES:1|4:NO:1",
                        "maker_entry_net_edge": "-0.01",
                        "taker_net_edge": "-0.03",
                        "raw_expected_edge": "0.02",
                    },
                ]
            },
        }
        result = analyze(payload)
        self.assertEqual(result["maker_positive_candidates"], 1)
        self.assertEqual(result["rows"][0]["market"], "positive")
        self.assertEqual(result["rows"][0]["legs"], 2)

    def test_iid_floor_is_only_defined_for_valid_bundle_probability(self) -> None:
        self.assertIsNone(iid_per_leg_fill_floor(None, 8))
        self.assertIsNone(iid_per_leg_fill_floor(0.8, 0))
        self.assertIsNone(break_even_completion_probability(-0.01, -0.03))
        self.assertIsNone(break_even_completion_probability(0.01, 0.0))


if __name__ == "__main__":
    unittest.main()
