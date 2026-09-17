from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v7_lead_lag_capacity_replay.py"
SPEC = importlib.util.spec_from_file_location("capacity_replay", SCRIPT)
assert SPEC and SPEC.loader
capacity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity
SPEC.loader.exec_module(capacity)


def event(event_type: str, order_id: str, market: str, **kwargs):
    row = {
        "event_type": event_type,
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "order_id": order_id,
        "market_id": market,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "metadata": {"model_family": "lead_lag_taker_v1", "paper_forward_test": True},
    }
    row.update(kwargs)
    return row


def trade_rows(order_id: str, market: str, *, ask: float, visible: float, won: bool, opened: int):
    fee_per_share = 0.01
    pnl_per_share = (1.0 if won else 0.0) - ask - fee_per_share
    order = event(
        "ORDER_SUBMITTED", order_id, market,
        ask=ask, ask_depth=visible + 100,
        recorded_ts_ms=opened, book_snapshot_id="snapshot-" + market,
    )
    order["metadata"].update({
        "arrival_best_ask": ask,
        "arrival_best_ask_size": visible,
        "arrival_fee_per_share": fee_per_share,
    })
    fill = event(
        "FILL", order_id, market,
        filled_size=5.0, fee=5.0 * fee_per_share,
        recorded_ts_ms=opened + 1,
    )
    final = event(
        "FINAL", order_id, market,
        final_pnl=5.0 * pnl_per_share,
        recorded_ts_ms=opened + 1000,
    )
    final["metadata"]["won"] = won
    return order, fill, final


class CapacityReplayTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.rows += trade_rows("o1", "m1", ask=0.20, visible=10.0, won=True, opened=1000)
        self.rows += trade_rows("o2", "m2", ask=0.80, visible=100.0, won=False, opened=1500)

    def write_ledger(self, root: Path) -> Path:
        path = root / "execution.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")
        return path

    def test_collects_settled_paper_forward_trades(self):
        with tempfile.TemporaryDirectory() as directory:
            trades = capacity.collect_trades(self.write_ledger(Path(directory)))
        self.assertEqual(len(trades), 2)
        self.assertEqual([trade.top_visible_shares for trade in trades], [10.0, 100.0])
        self.assertAlmostEqual(trades[0].pnl_per_share, 0.79)
        self.assertAlmostEqual(trades[1].pnl_per_share, -0.81)

    def test_strict_size_never_sweeps_beyond_best_ask(self):
        with tempfile.TemporaryDirectory() as directory:
            trades = capacity.collect_trades(self.write_ledger(Path(directory)))
        result = capacity.scenario(trades, target=25, dimension="shares", mode="strict", bootstrap_draws=0, seed=1)
        self.assertEqual(result["executed_markets"], 1)
        self.assertEqual(result["full_fill_markets"], 1)
        self.assertEqual(result["total_filled_shares"], 25)
        self.assertAlmostEqual(result["realized_pnl_usd"], -20.25)

    def test_clip_mode_uses_only_observed_best_ask_quantity(self):
        with tempfile.TemporaryDirectory() as directory:
            trades = capacity.collect_trades(self.write_ledger(Path(directory)))
        result = capacity.scenario(trades, target=25, dimension="shares", mode="clip", bootstrap_draws=0, seed=1)
        self.assertEqual(result["executed_markets"], 2)
        self.assertEqual(result["clipped_markets"], 1)
        self.assertEqual(result["total_filled_shares"], 35)
        expected = 10 * 0.79 + 25 * -0.81
        self.assertAlmostEqual(result["realized_pnl_usd"], expected)

    def test_fixed_notional_converts_to_shares_at_observed_ask(self):
        with tempfile.TemporaryDirectory() as directory:
            trades = capacity.collect_trades(self.write_ledger(Path(directory)))
        result = capacity.scenario(trades, target=10, dimension="notional_usd", mode="strict", bootstrap_draws=0, seed=1)
        self.assertEqual(result["executed_markets"], 1)
        self.assertAlmostEqual(result["total_filled_shares"], 12.5)
        self.assertAlmostEqual(result["total_entry_notional_usd"], 10.0)

    def test_capacity_thresholds_are_empirical_not_interpolated(self):
        values = [5.0, 10.0, 100.0, 1000.0]
        self.assertEqual(capacity.empirical_threshold(values, 0.75), 10.0)
        self.assertEqual(capacity.empirical_threshold(values, 0.50), 100.0)
        self.assertEqual(capacity.empirical_threshold(values, 0.25), 1000.0)

    def test_real_order_evidence_is_rejected(self):
        self.rows[0]["real_order_submission"] = True
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                capacity.collect_trades(self.write_ledger(Path(directory)))

    def test_report_explicitly_refuses_unobserved_sweep_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            trades = capacity.collect_trades(self.write_ledger(Path(directory)))
        report = capacity.build_report(
            trades,
            share_grid=(5.0, 25.0),
            notional_grid=(5.0, 25.0),
            bootstrap_draws=100,
            seed=1,
        )
        boundary = report["evidence_boundary"]
        self.assertFalse(boundary["full_book_sweep_replay_available"])
        self.assertIn("full_polymarket_ask_ladder_at_arrival", boundary["not_retained_historically"])
        self.assertEqual(report["strict_fixed_share_grid"][1]["executed_markets"], 1)

    def test_peak_concurrent_capital_counts_overlap(self):
        rows = [(1000, 3000, 10.0), (2000, 4000, 20.0), (4000, 5000, 5.0)]
        self.assertAlmostEqual(capacity.peak_concurrent_cash(rows), 30.0)


if __name__ == "__main__":
    unittest.main()


class CandidateDenominatorTests(unittest.TestCase):
    def test_candidate_markets_include_rejected_zero_pnl_opportunities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            rows += trade_rows("o1", "m1", ask=0.2, visible=100.0, won=True, opened=1000)
            ledger = root / "ledger.jsonl"
            ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
            events = root / "events.jsonl"
            events.write_text("".join(json.dumps(row) + "\n" for row in [
                {"event": "CANDIDATE", "market_id": "m1"},
                {"event": "CANDIDATE", "market_id": "m2"},
                {"event": "REJECTED", "market_id": "m2", "reason": "ARRIVAL_NO_CHASE_OR_DEPTH"},
            ]))
            trades = capacity.collect_trades(ledger)
            evidence = capacity.collect_candidate_markets(events)
            report = capacity.build_report(trades, share_grid=(5.0,), notional_grid=(5.0,),
                                           bootstrap_draws=0, seed=1, candidate_evidence=evidence)
        self.assertEqual(report["candidate_markets"], 2)
        self.assertEqual(report["settled_reference_markets"], 1)
        row = report["strict_fixed_share_grid"][0]
        self.assertEqual(row["participation_rate"], 0.5)
        self.assertEqual(row["conditional_same_price_coverage"], 1.0)
