from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_complete_set_maker as maker
import v7_market_common as common

NOW = 1_787_820_000_000


def cfg() -> dict:
    return {
        "paper_only": True,
        "authenticated_execution": False,
        "paper_capital_usd": 10_000.0,
        "kelly_fraction": 0.25,
        "min_post_cost_edge": 0.00005,
        "min_fill_probability": 0.01,
        "min_joint_completion_probability": 0.0025,
        "joint_completion_haircut": 0.5,
        "flow_size_fraction": 0.25,
        "flow_lookback_seconds": 300,
        "ttl_seconds": 60,
        "cancel_latency_ms": 250,
        "capital_cost_bps_per_hour": 0.0,
        "adverse_markout_fraction": 0.10,
        "expected_partial_fraction": 0.25,
    }


def book(token: str) -> maker.FullBook:
    return maker.FullBook(
        token,
        ((0.47, 1000.0),),
        ((0.50, 1000.0),),
        0.01,
        1.0,
        NOW - 100,
        NOW - 50,
        f"hash-{token}",
    )


def fee() -> common.FeeDetails:
    return common.FeeDetails(0.0, 1.0, True, True, "test:verified")


def write_tape(path: Path) -> None:
    fields = ["timestamp", "received_ms", "asset_id", "side", "price", "size", "transaction_hash"]
    rows = [
        # Both valid contra-flow rows are known at decision time, inside the 300s
        # event-time window, but far below the fill-replay watermark.
        {"timestamp": (NOW - 45_000) / 1000, "received_ms": NOW - 44_000, "asset_id": "YES", "side": "SELL", "price": 0.47, "size": 100.0, "transaction_hash": "valid-y"},
        {"timestamp": (NOW - 46_000) / 1000, "received_ms": NOW - 44_500, "asset_id": "NO", "side": "SELL", "price": 0.47, "size": 100.0, "transaction_hash": "valid-n"},
        # Causally unavailable at decision time. These rows are deliberately above
        # the replay watermark to prove that watermarking and decision causality
        # are different contracts.
        {"timestamp": (NOW - 10_000) / 1000, "received_ms": NOW + 1, "asset_id": "YES", "side": "SELL", "price": 0.47, "size": 500.0, "transaction_hash": "future-receive"},
        {"timestamp": (NOW + 1_000) / 1000, "received_ms": NOW - 1, "asset_id": "NO", "side": "SELL", "price": 0.47, "size": 500.0, "transaction_hash": "future-event"},
        # Known but outside the configured event-time lookback.
        {"timestamp": (NOW - 301_000) / 1000, "received_ms": NOW - 300_000, "asset_id": "YES", "side": "SELL", "price": 0.47, "size": 500.0, "transaction_hash": "too-old"},
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class RollingDecisionFlowTest(unittest.TestCase):
    def test_decision_window_is_independent_of_fill_replay_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / "trades.csv"
            write_tape(tape)
            state = {
                "tape_watermark_received_ms": NOW - 5_000,
                "tape_watermark_trade_ids": [],
            }
            incremental = maker.read_new_tape(tape, state)
            rolling = maker.read_decision_tape_window(
                tape,
                decision_ms=NOW,
                lookback_seconds=300,
            )

        # Fill replay is a receive-watermark contract. It may still read rows that
        # a later order-causality check will reject, but it must not replay the
        # already-consumed 45s/46s rows.
        self.assertEqual({row.event_ts_ms for row in incremental}, {NOW - 10_000, NOW + 1_000})
        self.assertNotIn(NOW - 45_000, {row.event_ts_ms for row in incremental})
        self.assertNotIn(NOW - 46_000, {row.event_ts_ms for row in incremental})

        # Decision memory is a separate causal window: only information locally
        # known by NOW and whose market event is inside the 300s lookback survives.
        self.assertEqual({row.event_ts_ms for row in rolling}, {NOW - 45_000, NOW - 46_000})
        self.assertEqual({row.token_id for row in rolling}, {"YES", "NO"})
        self.assertEqual(len(rolling), 2)
        self.assertTrue(all(row.received_ms <= NOW for row in rolling))
        self.assertTrue(all(NOW - 300_000 <= row.event_ts_ms <= NOW for row in rolling))

    def test_below_watermark_flow_changes_admission_without_replaying_fill(self) -> None:
        market = maker.Market("m", "e", "c", "YES", "NO", 100.0, {"feesEnabled": False})
        yes, no = book("YES"), book("NO")
        with tempfile.TemporaryDirectory() as tmp:
            tape = Path(tmp) / "trades.csv"
            write_tape(tape)
            state = {
                "tape_watermark_received_ms": NOW - 5_000,
                "tape_watermark_trade_ids": [],
            }
            incremental = maker.read_new_tape(tape, state)
            rolling = maker.read_decision_tape_window(tape, decision_ms=NOW, lookback_seconds=300)

        # The two causal 45s/46s observations have already crossed the fill replay
        # watermark and therefore cannot create a second fill.
        self.assertNotIn(NOW - 45_000, {row.event_ts_ms for row in incremental})
        self.assertNotIn(NOW - 46_000, {row.event_ts_ms for row in incremental})

        # With no causal flow memory, flow-capped sizing abstains. Restoring the
        # same below-watermark observations to the decision-only window admits a
        # positive-EV PAPER candidate without replaying either print as a fill.
        self.assertIsNone(maker.choose_quote(market, yes, no, yes_fee=fee(), no_fee=fee(), recent_trades=[], cfg=cfg()))
        quote = maker.choose_quote(market, yes, no, yes_fee=fee(), no_fee=fee(), recent_trades=rolling, cfg=cfg())
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertGreater(quote["expected_bundle_ev"], 0.0)
        self.assertGreater(quote["joint_completion_probability"], 0.0)

    def test_run_cycle_source_uses_rolling_reader_for_decisions_only(self) -> None:
        source = (SCRIPTS / "v7_complete_set_maker.py").read_text(encoding="utf-8")
        self.assertIn("recent_for_decision = read_decision_tape_window(", source)
        self.assertNotIn("recent_for_decision = [t for t in new_trades", source)
        self.assertIn("for trade in new_trades:", source)


if __name__ == "__main__":
    unittest.main()
