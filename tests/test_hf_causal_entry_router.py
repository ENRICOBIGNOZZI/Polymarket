from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.hf_causal_entry_router import Observation, route_observations, summarize


class HFCausalEntryRouterTest(unittest.TestCase):
    def _obs(
        self,
        decision: float,
        outcome: float,
        *,
        predicted: float = 0.01,
        future: float = 0.54,
        entry: float = 0.50,
        fee: float = 0.001,
        slip: float = 0.001,
        market: str = "",
        family: str = "micropressure",
        side: str = "YES",
        horizon: int = 60,
    ) -> Observation:
        return Observation(
            decision_ts=decision,
            outcome_ts=outcome,
            predicted_net_edge=predicted,
            executable_price=entry,
            future_side_mid=future,
            fee_per_share=fee,
            slippage_per_share=slip,
            quantity=10.0,
            market_id=market,
            signal_family=family,
            side=side,
            horizon_seconds=horizon,
        )

    def test_future_and_same_timestamp_outcomes_never_enter_history(self) -> None:
        rows = [self._obs(0, 20, market="a"), self._obs(20, 21, market="b")]
        decisions = route_observations(rows, min_history=2, min_distinct_markets=1)
        self.assertEqual(decisions[-1]["causal_history_count"], 0)
        self.assertEqual(decisions[-1]["route"], "MAKER_SHADOW")

    def test_taker_activates_after_positive_matched_cross_market_history(self) -> None:
        history = [self._obs(i, i + 1, market=f"m{i}") for i in range(12)]
        candidate = self._obs(20, 21, market="candidate")
        last = route_observations(history + [candidate], min_history=12, min_distinct_markets=6)[-1]
        self.assertEqual(last["causal_history_count"], 12)
        self.assertEqual(last["causal_distinct_markets"], 12)
        self.assertGreater(last["causal_stressed_lcb95"], 0.0)
        self.assertEqual(last["route"], "TAKER_PAPER")

    def test_unrelated_signal_family_cannot_authorize_taker(self) -> None:
        history = [self._obs(i, i + 1, market=f"m{i}", family="other") for i in range(12)]
        candidate = self._obs(20, 21, market="candidate", family="micropressure")
        last = route_observations(history + [candidate], min_history=12, min_distinct_markets=6)[-1]
        self.assertEqual(last["causal_history_count"], 0)
        self.assertEqual(last["route"], "MAKER_SHADOW")

    def test_other_side_or_horizon_cannot_authorize_taker(self) -> None:
        history = [self._obs(i, i + 1, market=f"m{i}", side="NO", horizon=5) for i in range(12)]
        candidate = self._obs(20, 21, market="candidate", side="YES", horizon=60)
        last = route_observations(history + [candidate], min_history=12, min_distinct_markets=6)[-1]
        self.assertEqual(last["causal_history_count"], 0)
        self.assertEqual(last["route"], "MAKER_SHADOW")

    def test_repeated_single_market_cannot_create_false_precision(self) -> None:
        history = [self._obs(i, i + 1, market="same-market") for i in range(20)]
        candidate = self._obs(30, 31, market="candidate")
        last = route_observations(history + [candidate], min_history=12, min_distinct_markets=6)[-1]
        self.assertEqual(last["causal_history_count"], 20)
        self.assertEqual(last["causal_distinct_markets"], 1)
        self.assertEqual(last["reason"], "insufficient_causal_forward_diversity")
        self.assertEqual(last["route"], "MAKER_SHADOW")

    def test_negative_stressed_history_routes_to_shadow(self) -> None:
        history = [
            self._obs(i, i + 1, market=f"m{i}", future=0.50)
            for i in range(12)
        ]
        candidate = self._obs(20, 21, market="candidate")
        last = route_observations(history + [candidate], min_history=12, min_distinct_markets=6)[-1]
        self.assertLess(last["causal_stressed_lcb95"], 0.0)
        self.assertEqual(last["route"], "MAKER_SHADOW")

    def test_negative_predicted_edge_is_never_forced(self) -> None:
        decision = route_observations([self._obs(0, 1, predicted=-0.001)], min_history=2)[0]
        self.assertEqual(decision["route"], "SKIP")

    def test_cost_stress_is_monotone(self) -> None:
        rows = [self._obs(0, 1, future=0.51)]
        summary = summarize(rows, route_observations(rows, min_history=2))
        self.assertGreater(summary["cost_stress"]["1x"]["pnl"], summary["cost_stress"]["1.5x"]["pnl"])
        self.assertGreater(summary["cost_stress"]["1.5x"]["pnl"], summary["cost_stress"]["2x"]["pnl"])


if __name__ == "__main__":
    unittest.main()
