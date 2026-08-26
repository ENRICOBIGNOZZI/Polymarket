from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str((Path(__file__).resolve().parents[1] / "scripts").resolve()))

import hf_active_flow_maker_core as core
import hf_fill_conditioned_maker_seeded_probe as probe


def args():
    return SimpleNamespace(
        starting_capital=1200.0,
        max_order_usd=125.0,
        max_market_fraction=0.05,
        max_event_fraction=0.15,
        max_gross_fraction=0.70,
        min_edge=0.00005,
        min_recent_trades=1,
        min_sell_prints=1,
        max_event_age_seconds=90,
        min_fill_probability=0.005,
        max_sell_toxicity=0.80,
        recent_lookback_seconds=120,
        toxicity_mult=0.25,
        improve_ticks=1,
        markets=1000,
        activity_scan_markets=120,
    )


def market() -> core.Market:
    return core.Market(
        "m1", "c1", "e1", "slug", "yes", "no", 1000.0, 1000.0,
        core.Fee(0.0, 1.0, True, "test"),
    )


def book() -> core.Book:
    return core.Book(
        "yes",
        [core.Level(0.50, 10.0), core.Level(0.49, 40.0)],
        [core.Level(0.53, 20.0), core.Level(0.54, 40.0)],
        0.01,
        1.0,
    )


def source_candidate(confidence: float = 0.90) -> core.Candidate:
    flow = core.Flow(4, 30.0, 10.0, 15.0, 2, 1, 0.5)
    return core.Candidate(
        "active_flow", market(), "YES", "yes", 0.01, 0.50, 5.0, 10.0,
        0.025, 0.025, confidence, 0, flow, 0.30, 0.0075,
    )


def resting_order(created_ts: int = 1000, queue: float = 100.0, shares: float = 10.0) -> core.ShadowOrder:
    c = source_candidate()
    c.limit_price = 0.50
    c.shares = shares
    c.queue_ahead = queue
    c.adjusted_edge = 0.01
    return core.ShadowOrder(c, created_ts, created_ts * 1000, created_ts + 60, shares, queue)


class FillConditionedMakerSeededProbeTest(unittest.TestCase):
    def tearDown(self):
        probe._ACTIVE_ARGS = None
        probe._REVALIDATION_STATS.clear()

    def test_prior_is_strictly_pre_decision(self):
        with self.assertRaises(RuntimeError):
            probe.fill_conditioned_gate([], {}, {}, probe.PRIOR_CUTOFF_TS, args())

    def test_safe_touch_candidate_is_live_wired(self):
        c = source_candidate()
        trades = {
            "c1": [
                core.Trade("b1", "yes", "BUY", 0.51, 30.0, probe.PRIOR_CUTOFF_TS + 10),
                core.Trade("s1", "yes", "SELL", 0.50, 20.0, probe.PRIOR_CUTOFF_TS + 11),
            ]
        }
        routed, stats = probe.fill_conditioned_gate(
            [c], {"yes": book()}, trades, probe.PRIOR_CUTOFF_TS + 20, args()
        )
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0].improvement_ticks, 0)
        self.assertEqual(stats["router_post_at_touch"], 1)
        self.assertFalse(stats["future_current_window_markout_used_for_admission"])

    def test_low_confidence_inside_never_gets_quote_improvement(self):
        c = source_candidate(confidence=0.60)
        trades = {
            "c1": [
                core.Trade("b1", "yes", "BUY", 0.51, 60.0, probe.PRIOR_CUTOFF_TS + 10),
                core.Trade("s1", "yes", "SELL", 0.51, 20.0, probe.PRIOR_CUTOFF_TS + 11),
                core.Trade("s2", "yes", "SELL", 0.51, 20.0, probe.PRIOR_CUTOFF_TS + 12),
                core.Trade("b2", "yes", "BUY", 0.51, 60.0, probe.PRIOR_CUTOFF_TS + 13),
            ]
        }
        routed, _ = probe.fill_conditioned_gate(
            [c], {"yes": book()}, trades, probe.PRIOR_CUTOFF_TS + 20, args()
        )
        self.assertTrue(all(item.improvement_ticks == 0 for item in routed))

    def test_recurrent_sell_toxicity_is_rejected(self):
        c = source_candidate()
        trades = {
            "c1": [
                core.Trade("s1", "yes", "SELL", 0.50, 30.0, probe.PRIOR_CUTOFF_TS + 10),
                core.Trade("s2", "yes", "SELL", 0.50, 30.0, probe.PRIOR_CUTOFF_TS + 11),
                core.Trade("s3", "yes", "SELL", 0.50, 30.0, probe.PRIOR_CUTOFF_TS + 12),
                core.Trade("s4", "yes", "SELL", 0.50, 30.0, probe.PRIOR_CUTOFF_TS + 13),
            ]
        }
        routed, stats = probe.fill_conditioned_gate(
            [c], {"yes": book()}, trades, probe.PRIOR_CUTOFF_TS + 20, args()
        )
        self.assertEqual(routed, [])
        self.assertGreater(stats["router_reason_counts"].get("directional_sell_flow_too_toxic", 0), 0)

    def test_profile_broadens_recent_flow_scan_without_lowering_edge_floor(self):
        a = args()
        a.min_recent_trades = 2
        a.min_sell_prints = 2
        a.max_event_age_seconds = 60
        a.min_fill_probability = 0.02
        a.improve_ticks = 3
        d = probe.apply_fill_conditioned_profile(a)
        self.assertEqual(a.improve_ticks, 1)
        self.assertEqual(a.activity_scan_markets, 300)
        self.assertEqual(a.min_edge, 0.00005)
        self.assertEqual(d["inside_markout_prior_per_share"], probe.INSIDE_MARKOUT_PRIOR)
        self.assertFalse(d["positive_at_touch_markout_credit"])

    def test_collapsed_hazard_requests_cancel_but_order_stays_fillable_during_latency(self):
        probe._ACTIVE_ARGS = args()
        order = resting_order(queue=0.0, shares=10.0)
        probe.rolling_revalidated_consume(order, [], 1030)
        self.assertEqual(order.remaining, 10.0)
        self.assertEqual(getattr(order, "hf_cancel_effective_ts"), 1031.0)

        # A public SELL whose event-time precedes cancel effectiveness still fills,
        # even though the next Data-API observation arrives after cancellation would
        # have become effective locally.
        trade = core.Trade("s-fill", "yes", "SELL", 0.50, 10.0, 1031)
        probe.rolling_revalidated_consume(order, [trade], 1032)
        self.assertEqual(order.remaining, 0.0)
        self.assertEqual(order.filled, 10.0)

    def test_trade_after_cancel_effective_time_does_not_fill(self):
        probe._ACTIVE_ARGS = args()
        order = resting_order(queue=0.0, shares=10.0)
        probe.rolling_revalidated_consume(order, [], 1030)
        trade = core.Trade("s-too-late", "yes", "SELL", 0.50, 10.0, 1032)
        probe.rolling_revalidated_consume(order, [trade], 1032)
        self.assertEqual(order.filled, 0.0)
        self.assertEqual(order.remaining, 0.0)

    def test_rolling_flow_can_preserve_queue_after_grace(self):
        probe._ACTIVE_ARGS = args()
        order = resting_order(queue=1000.0, shares=10.0)
        trades = [
            core.Trade("s1", "yes", "SELL", 0.50, 5.0, 1027),
            core.Trade("s2", "yes", "SELL", 0.50, 5.0, 1028),
            core.Trade("s3", "yes", "SELL", 0.50, 5.0, 1029),
        ]
        probe.rolling_revalidated_consume(order, trades, 1030)
        self.assertFalse(hasattr(order, "hf_cancel_effective_ts"))
        self.assertEqual(order.filled, 0.0)
        self.assertLess(order.queue_ahead, 1000.0)
        self.assertGreater(probe._REVALIDATION_STATS["kept"], 0)


if __name__ == "__main__":
    unittest.main()
