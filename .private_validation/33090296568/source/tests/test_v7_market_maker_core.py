#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_market_maker_core import (
    BookState,
    ExecutionEstimate,
    InventoryState,
    MakerPolicy,
    RewardContext,
    choose_outcome_quote,
    evaluate_quote,
    fair_value,
    post_only_price,
    reservation_price,
    reward_order_score,
)


class ProfessionalMakerCoreTests(unittest.TestCase):
    def book(self) -> BookState:
        return BookState(
            token_id="yes-token",
            bid=0.48,
            ask=0.52,
            bid_depth=500.0,
            ask_depth=300.0,
            tick_size=0.01,
            microprice=0.505,
            ofi=0.25,
            short_volatility=0.001,
            queue_bid=100.0,
            queue_ask=80.0,
            exchange_ts_ms=1_000,
            receive_ts_ms=1_010,
            snapshot_id="book-1",
        )

    def test_reward_score_is_quadratic_and_zero_outside_band(self) -> None:
        self.assertAlmostEqual(reward_order_score(3.0, 1.0, 100.0), (2.0 / 3.0) ** 2 * 100.0)
        self.assertEqual(reward_order_score(3.0, 3.1, 100.0), 0.0)

    def test_post_only_improvement_never_crosses(self) -> None:
        book = self.book()
        self.assertEqual(post_only_price(book, "BUY", "IMPROVE1", 1), 0.49)
        self.assertEqual(post_only_price(book, "SELL", "IMPROVE1", 1), 0.51)
        narrow = BookState("x", 0.49, 0.50, 10, 10, tick_size=0.01)
        self.assertIsNone(post_only_price(narrow, "BUY", "IMPROVE1", 1))
        self.assertIsNone(post_only_price(narrow, "SELL", "IMPROVE1", 1))

    def test_long_yes_inventory_skews_reservation_price_down(self) -> None:
        book = self.book()
        policy = MakerPolicy()
        fair = fair_value(book, policy)
        flat = reservation_price(fair, InventoryState(sleeve_capital=1000.0), book, policy)
        long_yes = reservation_price(
            fair,
            InventoryState(yes_shares=40.0, no_shares=0.0, sleeve_capital=1000.0),
            book,
            policy,
        )
        self.assertLess(long_yes, flat)

    def test_cold_start_exploration_does_not_need_directional_alpha(self) -> None:
        book = self.book()
        policy = MakerPolicy(
            exploration_enabled=True,
            cold_start_fill_prior=0.02,
            cold_start_adverse_markout_per_share=0.002,
            toxicity_withdraw_threshold=1.5,
        )
        quote = choose_outcome_quote(
            outcome="YES",
            book=book,
            fair=book.mid,
            reservation=book.mid,
            estimate=ExecutionEstimate(0.02, 0.002, observations=0, fills=0),
            reward=RewardContext(),
            inventory=InventoryState(sleeve_capital=1000.0),
            policy=policy,
        )
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertTrue(quote.exploration)
        self.assertFalse(quote.promotion_credit)
        self.assertLessEqual(quote.size * quote.price, 1.01)  # 0.1% of $1k sleeve

    def test_mature_negative_fill_conditioned_ev_abstains(self) -> None:
        book = self.book()
        policy = MakerPolicy(toxicity_withdraw_threshold=2.0)
        quote = choose_outcome_quote(
            outcome="YES",
            book=book,
            fair=book.mid,
            reservation=book.mid,
            estimate=ExecutionEstimate(
                fill_probability=0.60,
                adverse_markout_per_share=0.08,
                fill_uncertainty=0.1,
                observations=500,
                fills=200,
                event_clusters=30,
            ),
            reward=RewardContext(),
            inventory=InventoryState(sleeve_capital=1000.0),
            policy=policy,
        )
        self.assertIsNone(quote)

    def test_reward_pnl_is_separate_and_subsidy_dependency_is_explicit(self) -> None:
        book = self.book()
        policy = MakerPolicy(reward_haircut=0.5, rebate_haircut=0.5)
        reward = RewardContext(
            reward_qualified=True,
            max_spread_cents=5.0,
            min_size=1.0,
            pool_daily_usd=1000.0,
            estimated_competitor_score=10.0,
            maker_rebate_fraction=0.25,
            taker_fee_rate=0.01,
            expected_filled_maker_share=0.01,
        )
        quote = evaluate_quote(
            outcome="YES",
            side="BUY",
            action="IMPROVE1",
            book=book,
            fair=0.49,
            reservation=0.49,
            size=10.0,
            estimate=ExecutionEstimate(0.25, 0.01, observations=100, fills=30),
            reward=reward,
            companion_reward_score=10.0,
            inventory=InventoryState(sleeve_capital=1000.0),
            policy=policy,
            rest_seconds=60.0,
        )
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertLessEqual(quote.expected_trading_pnl, 0.0)
        self.assertGreater(quote.expected_liquidity_reward_pnl, 0.0)
        self.assertGreater(quote.expected_total_pnl, quote.expected_trading_pnl)
        self.assertTrue(quote.subsidy_dependent)


if __name__ == "__main__":
    unittest.main()
