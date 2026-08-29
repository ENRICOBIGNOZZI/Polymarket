from __future__ import annotations

import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_complete_set_maker as maker
import v7_execution_ledger as ledger
import v7_market_common as common

SHA = "b" * 40
NOW = 1_787_820_000_000


def cfg() -> dict:
    return {
        "paper_only": True,
        "authenticated_execution": False,
        "gamma_url": "https://gamma.invalid",
        "clob_url": "https://clob.invalid",
        "market_limit": 1000,
        "min_liquidity": 2.0,
        "paper_capital_usd": 10_000.0,
        "kelly_fraction": 0.25,
        "min_post_cost_edge": 0.00005,
        "min_fill_probability": 0.01,
        "min_joint_completion_probability": 0.0025,
        "joint_completion_haircut": 0.5,
        "flow_size_fraction": 0.25,
        "flow_lookback_seconds": 300,
        "ttl_seconds": 60,
        "submission_latency_ms": 50,
        "cancel_latency_ms": 250,
        "capital_cost_bps_per_hour": 0.0,
        "adverse_markout_fraction": 0.10,
        "expected_partial_fraction": 0.25,
        "max_book_age_ms": 5000,
        "max_cross_leg_exchange_skew_ms": 1500,
        "max_cross_leg_receive_skew_ms": 1500,
        "markout_max_delay_seconds": 15,
        "assumed_rewards_usd": 0.0,
        "candidate_market_ids": [{"market_id": "m", "event_id": "e", "prospective_not_before_ms": NOW - 1}],
    }


def book(token: str, bid: float, ask: float, *, bid_levels=None) -> maker.FullBook:
    bids = tuple(bid_levels or [(bid, 1000.0)])
    return maker.FullBook(token, bids, ((ask, 1000.0),), 0.01, 1.0, NOW - 100, NOW - 50, f"hash-{token}")


def fee(rate: float = 0.0) -> common.FeeDetails:
    return common.FeeDetails(rate=rate, exponent=1.0, taker_only=True, verified=True, source="test:verified")


class CompleteSetMakerTest(unittest.TestCase):
    def test_joint_distribution_is_explicit_and_not_marginal_product(self) -> None:
        dist = maker.conservative_joint_distribution(0.8, 0.6, 0.5)
        self.assertAlmostEqual(sum(dist.probabilities.values()), 1.0)
        self.assertAlmostEqual(dist.probabilities[3], 0.4)
        self.assertNotAlmostEqual(dist.probabilities[3], 0.8 * 0.6)
        self.assertLessEqual(dist.probabilities[3], min(0.8, 0.6))

    def test_fifo_queue_is_consumed_once_across_public_prints(self) -> None:
        order = {
            "token_id": "YES", "limit_price": 0.48, "queue_remaining": 10.0,
            "target_shares": 5.0, "filled_shares": 0.0,
            "arrival_event_ms": 1000, "arrival_received_ms": 1000,
            "ttl_ms": 10_000, "cancel_latency_ms": 250, "cancel_effective_ms": None,
        }
        t1 = maker.TapeTrade("1", "YES", "SELL", 0.48, 7.0, 1100, 1100)
        t2 = maker.TapeTrade("2", "YES", "SELL", 0.48, 5.0, 1200, 1200)
        self.assertEqual(maker.apply_trade_to_order(t1, order), 0.0)
        self.assertAlmostEqual(order["queue_remaining"], 3.0)
        self.assertAlmostEqual(maker.apply_trade_to_order(t2, order), 2.0)
        self.assertAlmostEqual(order["queue_remaining"], 0.0)
        self.assertAlmostEqual(order["filled_shares"], 2.0)

    def test_old_event_backfilled_after_arrival_cannot_fill(self) -> None:
        order = {
            "token_id": "YES", "limit_price": 0.48, "queue_remaining": 0.0,
            "target_shares": 5.0, "filled_shares": 0.0,
            "arrival_event_ms": 2000, "arrival_received_ms": 2000,
            "ttl_ms": 10_000, "cancel_latency_ms": 250, "cancel_effective_ms": None,
        }
        old = maker.TapeTrade("old", "YES", "SELL", 0.47, 10.0, 1900, 2100)
        self.assertFalse(maker.trade_can_fill(old, order))
        self.assertEqual(maker.apply_trade_to_order(old, order), 0.0)

    def test_cancel_pending_remains_fillable_only_until_effective(self) -> None:
        order = {
            "token_id": "YES", "limit_price": 0.48, "queue_remaining": 0.0,
            "target_shares": 5.0, "filled_shares": 0.0,
            "arrival_event_ms": 1000, "arrival_received_ms": 1000,
            "ttl_ms": 1000, "cancel_latency_ms": 250, "cancel_effective_ms": 2250,
        }
        before = maker.TapeTrade("a", "YES", "SELL", 0.48, 2.0, 2100, 2200)
        after = maker.TapeTrade("b", "YES", "SELL", 0.48, 2.0, 2300, 2300)
        self.assertTrue(maker.trade_can_fill(before, order, cancel_effective_ms=2250))
        self.assertFalse(maker.trade_can_fill(after, order, cancel_effective_ms=2250))

    def test_quote_selection_uses_positive_joint_bundle_ev(self) -> None:
        market = maker.Market("m", "e", "c", "YES", "NO", 100.0, {"feesEnabled": False})
        yes, no = book("YES", 0.47, 0.50), book("NO", 0.47, 0.50)
        trades = []
        for i in range(20):
            trades.append(maker.TapeTrade(str(i), "YES", "SELL", 0.48, 100.0, NOW - 30_000 + i * 100, NOW - 30_000 + i * 100 + 1))
            trades.append(maker.TapeTrade(f"n{i}", "NO", "SELL", 0.48, 100.0, NOW - 30_000 + i * 100, NOW - 30_000 + i * 100 + 1))
        result = maker.choose_quote(market, yes, no, yes_fee=fee(), no_fee=fee(), recent_trades=trades, cfg=cfg())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreater(result["expected_bundle_ev"], 0.0)
        self.assertGreaterEqual(result["full_completion_edge_per_share"], 0.00005)
        self.assertGreaterEqual(result["joint_completion_probability"], 0.0025)
        self.assertEqual(result["joint_source"], "conservative_frechet_haircut_proxy")

    def test_partial_unwind_cost_components_are_disjoint_and_terminal_pnl_is_actual(self) -> None:
        bundle = {
            "bundle_id": "bundle", "market_id": "m", "event_id": "e", "target_shares": 1.0,
            "first_fill_ms": NOW - 1000, "cancel_requested_ms": NOW - 500,
            "cancel_effective_ms": NOW - 1, "final_recorded": False, "state": "CANCEL_PENDING",
            "legs": {
                "YES": {
                    "order_id": "oy", "leg_id": "YES", "token_id": "YES", "limit_price": 0.48,
                    "filled_shares": 1.0, "entry_notional": 0.48, "entry_fee": 0.0,
                    "cancel_reference_bid": 0.47, "fee_rate": 0.0, "fee_exponent": 1.0,
                    "fee_taker_only": True, "fee_source": "test:verified",
                },
                "NO": {
                    "order_id": "on", "leg_id": "NO", "token_id": "NO", "limit_price": 0.48,
                    "filled_shares": 0.0, "entry_notional": 0.0, "entry_fee": 0.0,
                    "cancel_reference_bid": 0.47, "fee_rate": 0.0, "fee_exponent": 1.0,
                    "fee_taker_only": True, "fee_source": "test:verified",
                },
            },
        }
        yes = book("YES", 0.45, 0.50, bid_levels=[(0.45, 0.5), (0.43, 0.5)])
        no = book("NO", 0.47, 0.50)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            with ledger.CanonicalLedgerWriter(path, writer_id="test", model_sha=SHA) as writer:
                ok = maker.settle_bundle(writer, bundle, {"YES": yes, "NO": no}, model_sha=SHA, now_ms=NOW, cfg=cfg())
            events = list(ledger.iter_events(path, expected_model_sha=SHA))
        self.assertTrue(ok)
        exit_event = next(event for event in events if event.event_type == "EXIT")
        final_event = next(event for event in events if event.event_type == "FINAL")
        self.assertAlmostEqual(exit_event.unwind_loss or 0.0, 0.01)
        self.assertAlmostEqual(exit_event.latency_cost or 0.0, 0.02)
        self.assertAlmostEqual(exit_event.slippage or 0.0, 0.01)
        self.assertAlmostEqual(exit_event.fill_price or 0.0, 0.44)
        self.assertAlmostEqual(final_event.final_pnl or 0.0, -0.04)
        self.assertTrue(final_event.metadata["cost_vector_complete"])
        self.assertTrue(final_event.metadata["unwind_accounted"])

    def test_full_completion_locks_one_dollar_without_unwind(self) -> None:
        bundle = {
            "bundle_id": "bundle", "market_id": "m", "event_id": "e", "target_shares": 1.0,
            "first_fill_ms": NOW - 1000, "cancel_requested_ms": None, "cancel_effective_ms": None,
            "final_recorded": False, "state": "RESTING",
            "legs": {
                "YES": {"order_id": "oy", "leg_id": "YES", "token_id": "YES", "limit_price": 0.48, "filled_shares": 1.0, "entry_notional": 0.48, "entry_fee": 0.0, "cancel_reference_bid": None, "fee_rate": 0.0, "fee_exponent": 1.0, "fee_taker_only": True, "fee_source": "test:verified"},
                "NO": {"order_id": "on", "leg_id": "NO", "token_id": "NO", "limit_price": 0.49, "filled_shares": 1.0, "entry_notional": 0.49, "entry_fee": 0.0, "cancel_reference_bid": None, "fee_rate": 0.0, "fee_exponent": 1.0, "fee_taker_only": True, "fee_source": "test:verified"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            with ledger.CanonicalLedgerWriter(path, writer_id="test", model_sha=SHA) as writer:
                self.assertTrue(maker.settle_bundle(writer, bundle, {"YES": book("YES", 0.47, 0.50), "NO": book("NO", 0.48, 0.51)}, model_sha=SHA, now_ms=NOW, cfg=cfg()))
            final_event = next(event for event in ledger.iter_events(path, expected_model_sha=SHA) if event.event_type == "FINAL")
        self.assertAlmostEqual(final_event.final_pnl or 0.0, 0.03)
        self.assertEqual(final_event.metadata["terminal_basis"], "guaranteed_binary_complete_set_payoff_plus_full_depth_unwind")

    def test_config_rejects_rewards_and_authority_violations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            bad = cfg()
            bad["assumed_rewards_usd"] = 1.0
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(maker.MakerContractError):
                maker.load_config(path)
            bad = cfg()
            bad["kelly_fraction"] = 0.26
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(maker.MakerContractError):
                maker.load_config(path)
            bad = cfg()
            bad["min_post_cost_edge"] = 0.00001
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(maker.MakerContractError):
                maker.load_config(path)

    def test_prospective_cohort_timestamp_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            bad = cfg()
            bad["candidate_market_ids"][0]["prospective_not_before_ms"] = 0
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(maker.MakerContractError):
                maker.load_config(path)


if __name__ == "__main__":
    unittest.main()
