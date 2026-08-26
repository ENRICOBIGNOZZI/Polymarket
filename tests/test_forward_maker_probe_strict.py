import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "forward_maker_probe_strict.py"
spec = importlib.util.spec_from_file_location("forward_maker_probe_strict", SCRIPT)
strict = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = strict
spec.loader.exec_module(strict)
base = strict.base


class StrictForwardMakerMarkoutTest(unittest.TestCase):
    @staticmethod
    def book(bid: float, ask: float = 0.60) -> base.Book:
        return base.Book(
            "tok",
            (base.Level(bid, 100.0),),
            (base.Level(ask, 100.0),),
            0.01,
            1.0,
        )

    def test_censored_horizon_never_falls_back_to_pre_horizon_final_book(self):
        snapshots = [(100, {"tok": self.book(0.50)}), (350, {"tok": self.book(0.40)})]
        self.assertIsNone(strict.strict_snapshot_after(snapshots, "tok", 400))
        # This is the exact legacy failure mode: the old helper returns the final
        # snapshot even though it is 50 seconds before the requested horizon.
        self.assertIsNotNone(base.snapshot_after(snapshots, "tok", 400))

    def test_exact_or_later_snapshot_is_observed(self):
        snapshots = [
            (100, {"tok": self.book(0.50)}),
            (144, {"tok": self.book(0.49)}),
            (145, {"tok": self.book(0.48)}),
            (161, {"tok": self.book(0.47)}),
        ]
        self.assertAlmostEqual(strict.strict_snapshot_after(snapshots, "tok", 145).best_bid, 0.48)
        self.assertAlmostEqual(strict.strict_snapshot_after(snapshots, "tok", 160).best_bid, 0.47)

    def test_replay_adds_45s_markout_and_strictly_censors_300s(self):
        leg = base.QuoteLeg("tok", "YES", 0.50, 10.0, 0.0, 100.0)
        trades = [base.Trade(100, "tok", "SELL", 0.50, 10.0, "fill")]
        snapshots = [
            (100, {"tok": self.book(0.50)}),
            (145, {"tok": self.book(0.49)}),
            (160, {"tok": self.book(0.48)}),
            (350, {"tok": self.book(0.45)}),
        ]
        replay = strict.strict_simulate_leg(leg, trades, snapshots)
        self.assertEqual(replay.filled_shares, 10.0)
        self.assertAlmostEqual(replay.markout_45_bid_per_share, -0.01)
        self.assertAlmostEqual(replay.markout_60_bid_per_share, -0.02)
        self.assertIsNone(replay.markout_300_bid_per_share)

    def test_policy_result_serializes_45s_markout_without_breaking_old_schema(self):
        leg = base.QuoteLeg("tok", "YES", 0.50, 10.0, 0.0, 100.0)
        replay = strict.strict_simulate_leg(
            leg,
            [base.Trade(100, "tok", "SELL", 0.50, 10.0, "fill")],
            [(100, {"tok": self.book(0.50)}), (145, {"tok": self.book(0.49)}), (400, {"tok": self.book(0.48)})],
        )
        empty = base.LegReplay(
            outcome="NO", token_id="other", limit_price=0.40, target_shares=10.0,
            initial_queue_ahead=0.0, compatible_sell_volume=0.0, queue_remaining=0.0,
            filled_shares=0.0, first_fill_ts=None, last_fill_ts=None,
            markout_60_bid_per_share=None, markout_300_bid_per_share=None,
            final_bid=0.40, final_mid=0.405, fee_rate=0.0,
        )
        setattr(empty, "markout_45_bid_per_share", None)
        result = strict.strict_policy_result(
            base={"market_id": "m", "condition_id": "c"},
            policy="join", yes=replay, no=empty, quote_start_ts=100,
            quote_end_ts=400, exit_slippage_bps=0.0, eligible_fraction=0.0,
        )
        self.assertAlmostEqual(result["yes"]["markout_45_bid_per_share"], -0.01)
        self.assertIsNone(result["no"]["markout_45_bid_per_share"])
        self.assertIn("markout_60_bid_per_share", result["yes"])
        self.assertIn("markout_300_bid_per_share", result["yes"])


if __name__ == "__main__":
    unittest.main()
