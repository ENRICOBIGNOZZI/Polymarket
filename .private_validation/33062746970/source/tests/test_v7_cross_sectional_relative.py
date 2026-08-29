from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_cross_sectional_rank_core as core
import v7_cross_sectional_relative as relative


class RelativePairContractTest(unittest.TestCase):
    def score(self, market_id: str, event_id: str, p: float, pred: float, sigma: float = 0.05):
        return core.ScoreRow(
            ts=1000,
            market_id=market_id,
            event_id=event_id,
            group="g",
            probability=p,
            features=(0.0,) * len(core.FEATURE_NAMES),
            predicted_logit_move=pred,
            sigma_logit=sigma,
        )

    def book(self, market_id: str, event_id: str, p: float, fee: float = 0.0):
        return core.BookEconomics(
            market_id=market_id,
            event_id=event_id,
            yes_bid=p - 0.005,
            yes_ask=p + 0.005,
            no_bid=1.0 - p - 0.005,
            no_ask=1.0 - p + 0.005,
            liquidity=1000.0,
            fee_rate=fee,
            fee_exponent=1.0,
            taker_only=True,
            authoritative_fee=True,
            received_ts=1000,
        )

    def test_neutral_weights_cost_one_and_cancel_common_logit_mode(self) -> None:
        wt, wb, delta = relative.neutral_share_weights(0.70, 0.35, 0.705, 0.655)
        self.assertAlmostEqual(wt * 0.705 + wb * 0.655, 1.0, places=12)
        self.assertAlmostEqual(wt * 0.70 * 0.30, wb * 0.35 * 0.65, places=12)
        base = relative.first_order_pair_markout(0.08, -0.03, delta, 0.0)
        for common in (-2.0, -0.5, 0.25, 1.5):
            self.assertAlmostEqual(
                relative.first_order_pair_markout(0.08, -0.03, delta, common),
                base,
                places=12,
            )
        self.assertGreater(base, 0.0)

    def test_completed_pair_uses_relative_spread_not_single_leg_direction(self) -> None:
        top = self.score("top", "e1", 0.70, 0.05)
        bottom = self.score("bottom", "e2", 0.40, -0.04)
        candidate = relative.completed_pair_candidate(
            top,
            bottom,
            self.book("top", "e1", 0.70),
            self.book("bottom", "e2", 0.40),
            horizon_seconds=7200,
            now=1000,
            min_liquidity=2.0,
            max_spread=0.25,
            slippage_bps_round_trip_leg=0.0,
            capital_cost_bps_per_hour=0.0,
            adverse_penalty_bps=0.0,
            max_book_age_seconds=30,
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.top_side, "YES")
        self.assertEqual(candidate.bottom_side, "NO")
        self.assertAlmostEqual(candidate.predicted_relative_logit_spread, 0.09)
        self.assertGreater(candidate.predicted_gross_markout, 0.0)
        self.assertGreater(candidate.round_trip_spread_cost, 0.0)

    def test_pair_selection_uses_non_overlapping_extremes_and_event_dedup(self) -> None:
        scored = [
            self.score("m1", "e1", 0.50, -0.20),
            self.score("m2", "e2", 0.50, -0.10),
            self.score("m3", "e3", 0.50, 0.10),
            self.score("m4", "e4", 0.50, 0.20),
        ]
        books = {row.market_id: self.book(row.market_id, row.event_id, 0.50) for row in scored}
        pairs = relative.select_relative_pairs(
            scored,
            books,
            tail_fraction=0.25,
            horizon_seconds=7200,
            now=1000,
            minimum_completed_pair_net_edge=-1.0,
            max_pairs=2,
            maximum_pair_notional_usd=60.0,
            shadow_sleeve_budget_usd=100.0,
            one_contract_per_event=True,
            min_liquidity=2.0,
            max_spread=0.25,
            slippage_bps_round_trip_leg=0.0,
            capital_cost_bps_per_hour=0.0,
            adverse_penalty_bps=0.0,
            max_book_age_seconds=30,
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].top_market_id, "m4")
        self.assertEqual(pairs[0].bottom_market_id, "m1")
        self.assertLessEqual(pairs[0].max_pair_notional, 60.0)

    def test_joint_execution_ev_requires_explicit_four_state_distribution(self) -> None:
        candidate = relative.RelativePairCandidate(
            top_market_id="t",
            bottom_market_id="b",
            top_event_id="et",
            bottom_event_id="eb",
            horizon_seconds=7200,
            top_side="YES",
            bottom_side="NO",
            top_shares_per_pair_dollar=1.0,
            bottom_shares_per_pair_dollar=1.0,
            common_logit_delta_per_pair_dollar=0.2,
            predicted_top_relative_logit=0.1,
            predicted_bottom_relative_logit=-0.1,
            predicted_relative_logit_spread=0.2,
            predicted_gross_markout=0.04,
            round_trip_spread_cost=0.01,
            fees=0.0,
            slippage=0.0,
            capital_cost=0.0,
            adverse_penalty=0.0,
            completed_pair_net_edge=0.03,
            uncertainty_pnl_upper_bound=0.02,
            economic_score=1.5,
            max_pair_notional=50.0,
        )
        states = relative.JointFillStates(both=0.40, top_only=0.10, bottom_only=0.20, none=0.30)
        ev = relative.joint_execution_ev(
            candidate,
            states,
            top_only_unwind_loss_per_pair_dollar=0.02,
            bottom_only_unwind_loss_per_pair_dollar=0.03,
            capital_latency_cost_per_pair_dollar=0.001,
        )
        expected = 0.40 * 0.03 * 50 - 0.10 * 0.02 * 50 - 0.20 * 0.03 * 50 - 0.001 * 50
        self.assertAlmostEqual(ev.ev, expected)
        with self.assertRaises(ValueError):
            relative.JointFillStates(0.5, 0.5, 0.5, 0.0).validate()


if __name__ == "__main__":
    unittest.main()
