from __future__ import annotations

import argparse
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "scripts")))

import hf_active_flow_maker_batched_probe as batched  # noqa: E402
import hf_active_flow_maker_probe as entrypoint  # noqa: E402
from hf_active_flow_maker_batched_probe import gate_inside_improvements  # noqa: E402
from hf_active_flow_maker_probe import (  # noqa: E402
    Book,
    Candidate,
    Fee,
    Flow,
    Level,
    Market,
    ShadowOrder,
    Trade,
    activity_data_healthy,
    activity_eligible,
    build_candidates,
    consume,
    fee_per_share,
    fetch_trades_batch,
    fill_probability_proxy,
    flow_stats,
    select_with_caps,
)


def args() -> argparse.Namespace:
    return argparse.Namespace(
        starting_capital=1200.0,
        max_order_usd=125.0,
        max_market_fraction=0.05,
        max_event_fraction=0.15,
        max_gross_fraction=0.70,
        max_drawdown=0.15,
        max_spread=0.15,
        min_confidence=0.10,
        slippage_bps=5.0,
        adverse_selection_mult=0.15,
        toxicity_mult=0.25,
        improve_ticks=1,
        recent_lookback_seconds=120,
        min_recent_trades=2,
        min_sell_prints=2,
        max_event_age_seconds=60,
        min_fill_probability=0.02,
        max_sell_toxicity=0.80,
        min_edge=0.00005,
    )


def market(mid: str = "m1", event: str = "e1") -> Market:
    return Market(mid, "c-" + mid, event, "slug-" + mid, "yes-" + mid, "no-" + mid,
                  1000.0, 5000.0, Fee(0.0, 1.0, True, "test"))


def book(token: str, bid: float = 0.48, ask: float = 0.50,
         bid_size: float = 100.0, ask_size: float = 100.0, tick: float = 0.01) -> Book:
    return Book(token, [Level(bid, bid_size)], [Level(ask, ask_size)], tick, 1.0)


class ActiveFlowMakerProbeTest(unittest.TestCase):
    def test_entrypoint_injects_documented_trade_adapter_into_batched_runner(self):
        self.assertIs(batched.fetch_trades_batch, entrypoint.fetch_trades_batch)

    def test_batched_trade_fetch_uses_supported_query_and_filters_window_locally(self):
        rows = [
            {"conditionId": "c1", "asset": "yes-m1", "side": "SELL", "timestamp": 899,
             "price": 0.48, "size": 99.0, "transactionHash": "old"},
            {"conditionId": "c1", "asset": "yes-m1", "side": "SELL", "timestamp": 990,
             "price": 0.48, "size": 10.0, "transactionHash": "h1"},
            {"conditionId": "c2", "asset": "yes-m2", "side": "BUY", "timestamp": 991,
             "price": 0.51, "size": 7.0, "transactionHash": "h2"},
            {"conditionId": "c1", "asset": "yes-m1", "side": "SELL", "timestamp": 990,
             "price": 0.48, "size": 10.0, "transactionHash": "h1"},
            {"conditionId": "c1", "asset": "yes-m1", "side": "SELL", "timestamp": 1001,
             "price": 0.47, "size": 88.0, "transactionHash": "future"},
            {"conditionId": "outside", "asset": "x", "side": "SELL", "timestamp": 992,
             "price": 0.40, "size": 99.0, "transactionHash": "h3"},
        ]
        with patch("hf_active_flow_maker_probe.core.request_json") as request:
            request.return_value = (rows, 1_234_500)
            grouped, received_ms, errors = fetch_trades_batch(["c1", "c2"], 900, 1000, batch_size=20)
        self.assertEqual(errors, [])
        self.assertEqual(received_ms, 1_234_500)
        self.assertEqual(len(grouped["c1"]), 1)
        self.assertEqual(grouped["c1"][0].trade_id.split(":")[0], "h1")
        self.assertEqual(len(grouped["c2"]), 1)
        self.assertNotIn("outside", grouped)
        url = request.call_args.args[0]
        self.assertIn("market=c1,c2", url)
        self.assertIn("limit=1000", url)
        self.assertNotIn("start=", url)
        self.assertNotIn("end=", url)

    def test_retryable_batch_timeout_splits_to_single_conditions(self):
        rows1 = [{"conditionId": "c1", "asset": "t1", "side": "SELL", "timestamp": 990,
                  "price": 0.48, "size": 10.0, "transactionHash": "a"}]
        rows2 = [{"conditionId": "c2", "asset": "t2", "side": "BUY", "timestamp": 991,
                  "price": 0.51, "size": 7.0, "transactionHash": "b"}]
        timeout = urllib.error.HTTPError("https://data-api.polymarket.com/trades", 408, "timeout", {}, None)
        with patch("hf_active_flow_maker_probe.core.request_json") as request:
            request.side_effect = [timeout, (rows1, 1_000_100), (rows2, 1_000_200)]
            grouped, received_ms, errors = fetch_trades_batch(["c1", "c2"], 900, 1000, batch_size=2)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(errors, [])
        self.assertEqual(received_ms, 1_000_200)
        self.assertEqual(len(grouped["c1"]), 1)
        self.assertEqual(len(grouped["c2"]), 1)

    def test_transport_errors_cannot_silently_become_zero_activity_evidence(self):
        bad = {"universe": {"discovered_markets": 1000, "active_markets_evaluated": 0,
                             "flow_errors": ["batch=0:HTTPError:408"]}}
        self.assertFalse(activity_data_healthy(bad))
        real_zero = {"universe": {"discovered_markets": 1000, "active_markets_evaluated": 0,
                                   "flow_errors": []}}
        self.assertTrue(activity_data_healthy(real_zero))
        active = {"universe": {"discovered_markets": 1000, "active_markets_evaluated": 12,
                                "flow_errors": ["one recoverable single-condition failure"]}}
        self.assertTrue(activity_data_healthy(active))

    def test_fee_formula_matches_engine_per_share_contract(self):
        fee = Fee(0.07, 1.0, True, "test")
        self.assertAlmostEqual(fee_per_share(0.4, fee), 0.07 * 0.4 * 0.6)
        self.assertEqual(fee_per_share(0.4, Fee(0.0, 1.0, True, "zero")), 0.0)

    def test_fill_proxy_requires_recurrence_not_one_large_print(self):
        one = Flow(1, 0.0, 1000.0, 1000.0, 1, 1, -1.0)
        three = Flow(3, 0.0, 1000.0, 1000.0, 3, 1, -1.0)
        self.assertAlmostEqual(fill_probability_proxy(one, 100.0, 10.0), 1.0 / 3.0)
        self.assertEqual(fill_probability_proxy(three, 100.0, 10.0), 1.0)

    def test_activity_gate_rejects_dead_and_extremely_toxic_flow(self):
        a = args()
        dead = Flow(0, 0.0, 0.0, 0.0, 0, None, 0.0)
        self.assertFalse(activity_eligible(dead, 10.0, 10.0, a))
        toxic = Flow(4, 0.0, 100.0, 100.0, 4, 1, -1.0)
        self.assertFalse(activity_eligible(toxic, 10.0, 10.0, a))
        healthy = Flow(4, 60.0, 40.0, 40.0, 2, 1, 0.2)
        self.assertTrue(activity_eligible(healthy, 10.0, 10.0, a))

    def test_flow_stats_uses_event_time_window_and_compatible_sell_price(self):
        rows = [
            Trade("old", "tok", "SELL", 0.48, 100.0, 870),
            Trade("s1", "tok", "SELL", 0.48, 10.0, 950),
            Trade("s2", "tok", "SELL", 0.51, 20.0, 980),
            Trade("b1", "tok", "BUY", 0.50, 30.0, 990),
            Trade("future", "tok", "SELL", 0.47, 100.0, 1001),
        ]
        flow = flow_stats(rows, "tok", 1000, 120, 0.49)
        self.assertEqual(flow.trade_count, 3)
        self.assertEqual(flow.compatible_sell_prints, 1)
        self.assertAlmostEqual(flow.compatible_sell_volume, 10.0)
        self.assertEqual(flow.last_event_age, 10)

    def test_baseline_can_select_static_book_but_active_requires_recent_flow(self):
        a = args()
        m = market()
        books = {m.yes_token: book(m.yes_token), m.no_token: book(m.no_token)}
        baseline, active = build_candidates([m], books, {}, 1000, {m.market_id}, a)
        self.assertGreaterEqual(len(baseline), 1)
        self.assertEqual(active, [])

    def test_active_flow_candidate_is_created_from_recurrent_balanced_flow(self):
        a = args()
        m = market()
        books = {m.yes_token: book(m.yes_token), m.no_token: book(m.no_token)}
        trades = [
            Trade("s1", m.yes_token, "SELL", 0.48, 40.0, 980),
            Trade("s2", m.yes_token, "SELL", 0.48, 40.0, 990),
            Trade("b1", m.yes_token, "BUY", 0.50, 40.0, 985),
            Trade("b2", m.yes_token, "BUY", 0.50, 40.0, 995),
        ]
        _, active = build_candidates([m], books, {m.condition_id: trades}, 1000, {m.market_id}, a)
        self.assertTrue(any(c.side == "YES" for c in active))
        yes = next(c for c in active if c.side == "YES")
        self.assertGreater(yes.fill_probability_proxy, 0.02)
        self.assertGreater(yes.adjusted_edge, a.min_edge)

    def test_inside_spread_is_not_taken_when_tick_consumes_edge(self):
        a = args()
        m = market()
        books = {m.yes_token: book(m.yes_token, 0.48, 0.50), m.no_token: book(m.no_token, 0.48, 0.50)}
        trades = [
            Trade("s1", m.yes_token, "SELL", 0.48, 50.0, 990),
            Trade("s2", m.yes_token, "SELL", 0.48, 50.0, 995),
            Trade("b1", m.yes_token, "BUY", 0.50, 100.0, 996),
        ]
        _, active = build_candidates([m], books, {m.condition_id: trades}, 1000, {m.market_id}, a)
        yes = next(c for c in active if c.side == "YES")
        self.assertEqual(yes.improvement_ticks, 0)
        self.assertAlmostEqual(yes.limit_price, 0.48)

    def test_inside_spread_requires_incremental_fill_weighted_edge(self):
        a = args()
        m = market()
        b = book(m.yes_token, bid=0.48, ask=0.51, bid_size=100.0, ask_size=100.0, tick=0.01)
        trades = [
            Trade("s1", m.yes_token, "SELL", 0.48, 40.0, 980),
            Trade("s2", m.yes_token, "SELL", 0.48, 40.0, 990),
            Trade("b1", m.yes_token, "BUY", 0.51, 80.0, 985),
            Trade("b2", m.yes_token, "BUY", 0.51, 80.0, 995),
        ]
        inside_flow = flow_stats(trades, m.yes_token, 1000, 120, 0.49)
        inside = Candidate(
            "active_flow", m, "YES", m.yes_token, 0.01, 0.49, 25.0, 0.0,
            0.0100, 0.0010, 0.8, 1, inside_flow,
            fill_probability_proxy(inside_flow, 0.0, 25.0), 0.0,
        )
        inside.score = inside.fill_probability_proxy * inside.adjusted_edge
        gated, stats = gate_inside_improvements(
            [inside], {m.yes_token: b}, {m.condition_id: trades}, 1000, a)
        self.assertEqual(stats["inside_considered"], 1)
        self.assertEqual(stats["inside_kept"], 0)
        self.assertEqual(stats["reverted_to_touch"], 1)
        self.assertEqual(len(gated), 1)
        self.assertEqual(gated[0].improvement_ticks, 0)
        self.assertAlmostEqual(gated[0].limit_price, 0.48)
        self.assertGreater(gated[0].score, inside.score)

    def test_fifo_consumption_is_post_decision_and_respects_actual_tick(self):
        a = args()
        m = market()
        flow = Flow(4, 20.0, 20.0, 20.0, 2, 1, 0.0)
        c = Candidate("active_flow", m, "YES", m.yes_token, 0.001, 0.500, 5.0, 10.0,
                      0.01, 0.01, 0.5, 0, flow, 0.2, 0.002)
        order = ShadowOrder(c, 1000, 1000000, 1060, 5.0, 10.0)
        trades = [
            Trade("old", m.yes_token, "SELL", 0.5000, 100.0, 1000),
            Trade("too-high", m.yes_token, "SELL", 0.5003, 100.0, 1001),
            Trade("q", m.yes_token, "SELL", 0.5000, 8.0, 1002),
            Trade("fill", m.yes_token, "SELL", 0.5000, 6.0, 1003),
        ]
        consume(order, trades, 1010)
        self.assertAlmostEqual(order.queue_ahead, 0.0)
        self.assertAlmostEqual(order.filled, 4.0)
        self.assertAlmostEqual(order.remaining, 1.0)
        self.assertEqual(order.first_fill_event_ts, 1003)

    def test_portfolio_selection_enforces_event_and_gross_caps(self):
        a = args()
        f = Flow(4, 50, 50, 50, 2, 1, 0)
        candidates = []
        for i in range(10):
            m = market(f"m{i}", "same-event")
            candidates.append(Candidate("active_flow", m, "YES", m.yes_token, 0.01, 0.5,
                                        100.0, 0.0, 0.01, 0.01, 0.5, 0, f, 0.5, 0.005))
        selected = select_with_caps(candidates, 10, a)
        self.assertEqual(len(selected), 3)
        self.assertLessEqual(sum(c.shares * c.limit_price for c in selected), a.max_event_fraction * a.starting_capital)


if __name__ == "__main__":
    unittest.main()
