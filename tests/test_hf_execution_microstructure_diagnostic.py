import importlib.util
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hf_execution_microstructure_diagnostic.py"
spec = importlib.util.spec_from_file_location("hf_execution_microstructure_diagnostic", SCRIPT)
hf = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = hf
spec.loader.exec_module(hf)


class HFExecutionMicrostructureDiagnosticTest(unittest.TestCase):
    def test_microprice_depth_and_imbalance(self):
        book = {
            "tick_size": 0.01,
            "bids": [
                {"price": 0.49, "size": 100},
                {"price": 0.48, "size": 50},
                {"price": 0.47, "size": 25},
            ],
            "asks": [
                {"price": 0.51, "size": 50},
                {"price": 0.52, "size": 50},
                {"price": 0.53, "size": 100},
            ],
        }
        features = hf.book_features(book)
        self.assertTrue(features["valid"])
        self.assertAlmostEqual(features["midpoint"], 0.50)
        self.assertAlmostEqual(features["microprice"], (0.51 * 100 + 0.49 * 50) / 150)
        self.assertAlmostEqual(features["spread_ticks"], 2.0)
        self.assertAlmostEqual(features["imbalance_l1"], 1.0 / 3.0)
        self.assertAlmostEqual(features["l3_bid_depth"], 175.0)
        self.assertAlmostEqual(features["l3_ask_depth"], 200.0)

    def test_snapshot_ofi_proxy_detects_bid_improvement(self):
        previous = {
            "tick_size": 0.01,
            "bids": [{"price": 0.49, "size": 100}],
            "asks": [{"price": 0.51, "size": 50}],
        }
        current = {
            "tick_size": 0.01,
            "bids": [{"price": 0.50, "size": 120}],
            "asks": [{"price": 0.51, "size": 40}],
        }
        flow = hf.ofi_proxy(previous, current)
        self.assertTrue(flow["valid"])
        self.assertAlmostEqual(flow["ofi_l1_proxy"], 130.0)
        self.assertGreater(flow["ofi_l1_proxy_normalized"], 0.0)

    def test_b2_live_bundle_cannot_borrow_complete_set_fill_rate(self):
        live = {
            "git_sha": "abc",
            "generated_ts": 100,
            "b2_coherence": {
                "top_raw": [
                    {
                        "market": "2176270",
                        "slug": "x",
                        "legs": "2176270:NO:1|2774057:YES:2.5914",
                        "maker_entry_net_edge": "0.0222359",
                        "taker_net_edge": "-0.0238305",
                    }
                ]
            },
            "intents": {"bundles": 1},
        }
        probe = {
            "results": [
                {
                    "yes": {},
                    "no": {},
                    "source_locked_complete_set_edge": 0.01,
                }
            ]
        }
        calibration = {
            "by_policy": {
                "join": {
                    "sessions": 4,
                    "probes": 56,
                    "any_fills": 1,
                    "pair_fills": 0,
                    "one_sided_only": 1,
                    "pair_fill_rate_wilson_upper": 0.04608667759307948,
                    "total_pnl_ex_rewards_usd": -0.13894351315407616,
                    "filled_share_weighted_markout_60_bid_per_share": -0.002,
                    "eligible_for_paper_shadow": False,
                }
            }
        }
        report = hf.coverage_report(live, probe, calibration)
        self.assertEqual(report["active_live_maker_class"], "B2_multi_leg")
        self.assertEqual(report["forward_probe_class"], "two_leg_complete_set_reward")
        self.assertTrue(report["execution_evidence_class_mismatch"])
        self.assertFalse(report["cross_class_pair_fill_transfer_valid"])
        self.assertEqual(report["decision"], "MORE_EVIDENCE_REQUIRED")
        q = report["best_positive_b2"]["break_even_pair_completion_probability"]
        self.assertTrue(math.isclose(q, 0.5173076255144747, rel_tol=1e-12))

    def test_no_active_bundle_does_not_claim_mismatch(self):
        report = hf.coverage_report(
            {"b2_coherence": {"top_raw": []}, "intents": {"bundles": 0}},
            {"results": []},
            {"by_policy": {}},
        )
        self.assertFalse(report["execution_evidence_class_mismatch"])
        self.assertEqual(report["decision"], "NO_ACTIVE_MAKER_CHALLENGER")


if __name__ == "__main__":
    unittest.main()
