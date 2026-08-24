import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "analyze_forward_maker_fragility.py"
spec = importlib.util.spec_from_file_location("analyze_forward_maker_fragility", SCRIPT)
fragility = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = fragility
spec.loader.exec_module(fragility)


class ForwardMakerFragilityTest(unittest.TestCase):
    def test_one_sided_fill_exposes_queue_and_pair_completion_fragility(self):
        payload = {
            "schema": "polymarket_forward_maker_probe_v1",
            "generated_ts": 1,
            "results": [
                {
                    "market_id": "m1",
                    "policy": "join",
                    "any_fill": True,
                    "pair_fill": False,
                    "one_sided_only": True,
                    "matched_shares": 0.0,
                    "unmatched_yes_shares": 0.0,
                    "unmatched_no_shares": 10.0,
                    "locked_edge_per_matched_share": 0.001,
                    "conservative_pnl_ex_rewards_usd": -0.14,
                    "conditional_pnl_including_reward_usd": -0.136,
                    "conditional_prorated_reward_usd": 0.004,
                    "exit_fees_usd": 0.12,
                    "yes": {
                        "filled_shares": 0.0,
                        "initial_queue_ahead": 1000.0,
                        "compatible_sell_volume": 0.0,
                    },
                    "no": {
                        "filled_shares": 10.0,
                        "initial_queue_ahead": 100.0,
                        "compatible_sell_volume": 115.0,
                        "markout_60_bid_per_share": -0.002,
                        "markout_300_bid_per_share": None,
                    },
                }
            ],
        }
        report = fragility.analyze(payload)
        summary = report["policy_summaries"]["join"]
        leg = summary["filled_legs"][0]
        self.assertAlmostEqual(leg["queue_relative_headroom"], 0.15)
        self.assertAlmostEqual(leg["one_sided_downside_per_share"], 0.014)
        self.assertAlmostEqual(leg["break_even_pair_completion_probability"], 0.014 / 0.015)
        self.assertAlmostEqual(summary["exit_fee_fraction_of_one_sided_downside"], 0.12 / 0.14)
        self.assertAlmostEqual(summary["conditional_reward_offset_fraction"], 0.004 / 0.14)
        self.assertAlmostEqual(summary["filled_share_weighted_markout_60"], -0.002)
        self.assertEqual(report["diagnostics"]["break_even_pair_probability_ge_90pct_count"], 1)
        self.assertEqual(report["diagnostics"]["queue_headroom_le_25pct_count"], 1)
        self.assertEqual(report["diagnostics"]["negative_60s_markout_count"], 1)
        self.assertEqual(report["production_action"], "no_change")
        self.assertEqual(report["research_state"], "MORE_EVIDENCE_REQUIRED")

    def test_no_fill_policy_does_not_invent_execution_evidence(self):
        payload = {
            "results": [
                {
                    "market_id": "m2",
                    "policy": "fade1",
                    "any_fill": False,
                    "pair_fill": False,
                    "one_sided_only": False,
                    "conservative_pnl_ex_rewards_usd": 0.0,
                    "conditional_pnl_including_reward_usd": 0.001,
                    "conditional_prorated_reward_usd": 0.001,
                    "yes": {"filled_shares": 0.0},
                    "no": {"filled_shares": 0.0},
                }
            ]
        }
        report = fragility.analyze(payload)
        summary = report["policy_summaries"]["fade1"]
        self.assertEqual(summary["any_fill_count"], 0)
        self.assertIsNone(summary["pair_completion_given_any_fill"])
        self.assertIsNone(summary["maximum_break_even_pair_completion_probability"])
        self.assertEqual(report["diagnostics"]["filled_leg_count"], 0)

    def test_requires_result_rows(self):
        with self.assertRaises(ValueError):
            fragility.analyze({})


if __name__ == "__main__":
    unittest.main()
