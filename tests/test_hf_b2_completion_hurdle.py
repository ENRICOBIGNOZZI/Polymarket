#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hf_b2_completion_hurdle import (
    analyze,
    break_even_completion_probability,
    expected_edge_at_completion,
    iid_per_leg_fill_floor,
)


class HFB2CompletionHurdleTest(unittest.TestCase):
    def test_current_live_candidate_hurdle(self) -> None:
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
