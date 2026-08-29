import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "forward_maker_probe.py"
spec = importlib.util.spec_from_file_location("forward_maker_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


class ForwardMakerProbeTest(unittest.TestCase):
    def test_fifo_queue_fill_uses_only_future_compatible_taker_sells(self):
        leg = probe.QuoteLeg("tok", "YES", 0.40, 20.0, 100.0, 100.25)
        book = probe.Book(
            "tok",
            (probe.Level(0.40, 50.0),),
            (probe.Level(0.41, 50.0),),
            0.01,
            1.0,
        )
        later = probe.Book(
            "tok",
            (probe.Level(0.39, 50.0),),
            (probe.Level(0.40, 50.0),),
            0.01,
            1.0,
        )
        trades = [
            probe.Trade(100, "tok", "SELL", 0.40, 1_000.0, "too-early"),
            probe.Trade(101, "tok", "BUY", 0.40, 1_000.0, "wrong-side"),
            probe.Trade(102, "tok", "SELL", 0.41, 1_000.0, "too-expensive"),
            probe.Trade(103, "tok", "SELL", 0.40, 60.0, "deplete-1"),
            probe.Trade(104, "tok", "SELL", 0.39, 70.0, "deplete-and-fill"),
        ]
        replay = probe.simulate_leg(
            leg,
            trades,
            [(100, {"tok": book}), (164, {"tok": later}), (404, {"tok": later})],
            fee_rate=0.07,
        )
        self.assertEqual(replay.filled_shares, 20.0)
        self.assertEqual(replay.first_fill_ts, 104)
        self.assertEqual(replay.last_fill_ts, 104)
        self.assertEqual(replay.queue_remaining, 0.0)
        self.assertEqual(replay.compatible_sell_volume, 130.0)
        self.assertAlmostEqual(replay.markout_60_bid_per_share, -0.01)

    def test_pair_accounting_locks_complete_set_and_marks_unmatched_inventory(self):
        yes = probe.LegReplay(
            outcome="YES", token_id="y", limit_price=0.45, target_shares=10,
            initial_queue_ahead=0, compatible_sell_volume=10, queue_remaining=0,
            filled_shares=10, first_fill_ts=100, last_fill_ts=100,
            markout_60_bid_per_share=0.0, markout_300_bid_per_share=0.0,
            final_bid=0.44, final_mid=0.45, fee_rate=0.0,
        )
        no = probe.LegReplay(
            outcome="NO", token_id="n", limit_price=0.50, target_shares=10,
            initial_queue_ahead=0, compatible_sell_volume=5, queue_remaining=0,
            filled_shares=5, first_fill_ts=110, last_fill_ts=110,
            markout_60_bid_per_share=-0.01, markout_300_bid_per_share=-0.01,
            final_bid=0.49, final_mid=0.50, fee_rate=0.0,
        )
        result = probe.policy_result(
            base={"market_id": "m", "condition_id": "c", "estimated_native_daily_value": "8.64"},
            policy="join", yes=yes, no=no, quote_start_ts=0, quote_end_ts=100,
            exit_slippage_bps=0.0, eligible_fraction=0.5,
        )
        self.assertEqual(result["matched_shares"], 5.0)
        self.assertEqual(result["unmatched_yes_shares"], 5.0)
        self.assertAlmostEqual(result["locked_gross_pnl_usd"], 0.25)
        self.assertAlmostEqual(result["unmatched_liquidation_pnl_usd"], -0.05)
        self.assertAlmostEqual(result["conservative_pnl_ex_rewards_usd"], 0.20)
        self.assertAlmostEqual(result["conditional_prorated_reward_usd"], 0.005)
        self.assertEqual(result["pair_completion_delay_seconds"], 10)

    def test_quote_policies_do_not_cross(self):
        book = probe.Book(
            "tok",
            (probe.Level(0.40, 50.0),),
            (probe.Level(0.42, 50.0),),
            0.01,
            1.0,
        )
        self.assertAlmostEqual(probe.quote_price(book, "join"), 0.40)
        self.assertAlmostEqual(probe.quote_price(book, "improve1"), 0.41)
        self.assertAlmostEqual(probe.quote_price(book, "fade1"), 0.39)

    def test_candidate_selection_is_event_diverse_first(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.csv"
            path.write_text(
                "market_id,condition_id,event_id,quote_shares,locked_complete_set_edge,estimated_native_daily_value,market_competitiveness,volume24h\n"
                "m1,c1,e1,50,.03,0,1,100\n"
                "m2,c2,e1,50,.02,0,1,100\n"
                "m3,c3,e2,50,.01,0,1,100\n",
                encoding="utf-8",
            )
            selected = probe.load_candidates(path, 2, 1)
            self.assertEqual({row["event_id"] for row in selected}, {"e1", "e2"})


if __name__ == "__main__":
    unittest.main()
