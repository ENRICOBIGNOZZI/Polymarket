from __future__ import annotations

import unittest

from scripts.hf_causal_entry_router import Observation, route_observations, summarize


class HFCausalEntryRouterTest(unittest.TestCase):
    def _obs(
        self,
        decision: float,
        outcome: float,
        *,
        predicted: float = 0.01,
        future: float = 0.52,
        entry: float = 0.50,
        fee: float = 0.001,
        slip: float = 0.001,
        market: str = "",
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
        )

    def test_future_outcomes_never_enter_current_decision_history(self) -> None:
        rows = [
            self._obs(0, 100, market="a"),
            self._obs(10, 110, market="b"),
            self._obs(20, 120, market="c"),
        ]
        decisions = route_observations(rows, min_history=2)
        self.assertEqual([x["causal_history_count"] for x in decisions], [0, 0, 0])
        self.assertTrue(all(x["route"] == "MAKER_SHADOW" for x in decisions))

    def test_taker_activates_only_after_prior_positive_2x_markout_is_known(self) -> None:
        history = [
            self._obs(i, i + 1, future=0.54, entry=0.50, fee=0.001, slip=0.001, market=str(i))
            for i in range(12)
        ]
        candidate = self._obs(20, 21, market="candidate")
        decisions = route_observations(history + [candidate], min_history=12)
        last = decisions[-1]
        self.assertEqual(last["causal_history_count"], 12)
        self.assertGreater(last["causal_stressed_lcb95"], 0.0)
        self.assertEqual(last["route"], "TAKER_PAPER")

    def test_nonpositive_stressed_history_routes_to_maker_shadow(self) -> None:
        history = [
            self._obs(i, i + 1, future=0.50, entry=0.50, fee=0.001, slip=0.001, market=str(i))
            for i in range(12)
        ]
        candidate = self._obs(20, 21, market="candidate")
        last = route_observations(history + [candidate], min_history=12)[-1]
        self.assertLess(last["causal_stressed_lcb95"], 0.0)
        self.assertEqual(last["route"], "MAKER_SHADOW")

    def test_negative_predicted_edge_is_never_forced(self) -> None:
        row = self._obs(0, 1, predicted=-0.001)
        decision = route_observations([row], min_history=2)[0]
        self.assertEqual(decision["route"], "SKIP")

    def test_cost_stress_is_monotone(self) -> None:
        rows = [self._obs(0, 1, future=0.51, entry=0.50, fee=0.001, slip=0.001)]
        summary = summarize(rows, route_observations(rows, min_history=2))
        self.assertGreater(summary["cost_stress"]["1x"]["pnl"], summary["cost_stress"]["1.5x"]["pnl"])
        self.assertGreater(summary["cost_stress"]["1.5x"]["pnl"], summary["cost_stress"]["2x"]["pnl"])


if __name__ == "__main__":
    unittest.main()
