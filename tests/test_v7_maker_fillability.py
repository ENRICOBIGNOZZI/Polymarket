from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "monitoring" / "v7_maker_fillability.py"
SPEC = importlib.util.spec_from_file_location("v7_maker_fillability", MODULE_PATH)
assert SPEC and SPEC.loader
fillability = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fillability
SPEC.loader.exec_module(fillability)

SHA = "a" * 40


class V7MakerFillabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        fillability._CACHE_KEY = None
        fillability._CACHE_VALUE = None

    @staticmethod
    def _write_policy(path: Path) -> None:
        path.write_text(json.dumps({
            "paper_queue": {
                "assumed_submission_latency_ms": 1,
                "queue_ahead_multipliers": {"expected": 1.25, "upper": 1.5},
            },
            "quoting": {"min_quote_lifetime_ms": 100},
            "exploration": {"minimum_rest_ms": 250},
        }), encoding="utf-8")

    @staticmethod
    def _order(order_id: str = "o1", *, side: str = "BUY", queue: float = 5.0, limit: float = .50,
               exchange_ms: int = 100_000, decision_ms: int = 100_060) -> dict:
        return {
            "strategy": "MICRO_MAKER_PRO", "model_sha": SHA, "event_type": "ORDER_SUBMITTED",
            "order_id": order_id, "recorded_ts_ms": decision_ms + 5, "market_id": "m1",
            "token_id": "t1", "side": side, "intended_action": "JOIN", "intended_size": 2.0,
            "limit_price": limit, "bid": .50, "ask": .51, "queue_ahead": queue,
            "exchange_ts_ms": exchange_ms, "receive_ts_ms": decision_ms - 10,
            "decision_ts_ms": decision_ms, "metadata": {"action": "JOIN"},
        }

    @staticmethod
    def _state(order_id: str, state: str, ts_ms: int) -> dict:
        return {"strategy": "MICRO_MAKER_PRO", "model_sha": SHA, "event_type": "ORDER_STATE",
                "order_id": order_id, "order_state": state, "recorded_ts_ms": ts_ms}

    @staticmethod
    def _fill(order_id: str, size: float = 2.0) -> dict:
        return {"strategy": "MICRO_MAKER_PRO", "model_sha": SHA, "event_type": "FILL",
                "order_id": order_id, "filled_size": size, "recorded_ts_ms": 103_000}

    @staticmethod
    def _tape(path: Path, rows: list[tuple[int, int, str, str, float, float, str]]) -> None:
        lines = ["timestamp,received_ms,lag_ms,condition_id,asset_id,outcome,side,price,size,transaction_hash,slug,event_slug"]
        for ts_s, received_ms, token, side, price, size, trade_id in rows:
            lines.append(f"{ts_s},{received_ms},0,c,{token},YES,{side},{price},{size},{trade_id},s,e")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _run(self, ledger_rows: list[dict], tape_rows: list[tuple[int, int, str, str, float, float, str]], now_ms: int = 106_000):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "execution.jsonl"
            ledger.write_text("".join(json.dumps(row) + "\n" for row in ledger_rows), encoding="utf-8")
            tape = root / "trade_tape.csv"
            self._tape(tape, tape_rows)
            policy = root / "policy.json"
            self._write_policy(policy)
            return fillability.summarize_maker_fillability(ledger, tape, policy, model_sha=SHA, now_ms=now_ms)

    def test_opposite_aggressor_does_not_reach_maker_bid(self) -> None:
        out = self._run([self._order(), self._state("o1", "CANCELLED", 105_000)],
                        [(102, 102_100, "t1", "BUY", .50, 20.0, "tr1")])
        self.assertEqual(out["funnel"]["trade_reachable"], 0)
        self.assertEqual(out["orders"][0]["fillability_classification"], "NO_AGGRESSIVE_FLOW_REACHED_PRICE")

    def test_pre_arrival_trade_cannot_count(self) -> None:
        out = self._run([self._order(), self._state("o1", "CANCELLED", 105_000)],
                        [(99, 100_000, "t1", "SELL", .50, 20.0, "tr1")])
        self.assertEqual(out["funnel"]["trade_reachable"], 0)

    def test_flow_at_price_without_queue_depletion_is_classified(self) -> None:
        out = self._run([self._order(queue=10), self._state("o1", "CANCELLED", 105_000)],
                        [(102, 102_100, "t1", "SELL", .50, 3.0, "tr1")])
        self.assertEqual(out["funnel"]["trade_reachable"], 1)
        self.assertEqual(out["funnel"]["fill_opportunity_lower"], 0)
        self.assertEqual(out["orders"][0]["fillability_classification"], "AGGRESSIVE_FLOW_REACHED_PRICE_BUT_QUEUE_NOT_DEPLETED")

    def test_queue_uncertainty_scenarios_are_separated(self) -> None:
        out = self._run([self._order(queue=5), self._state("o1", "CANCELLED", 105_000)],
                        [(102, 102_100, "t1", "SELL", .50, 6.0, "tr1")])
        order = out["orders"][0]
        self.assertTrue(order["fill_opportunity_lower"])
        self.assertFalse(order["fill_opportunity_expected"])
        self.assertFalse(order["fill_opportunity_pessimistic"])
        self.assertEqual(order["fillability_classification"], "QUEUE_LOWER_DEPLETED_NOT_EXPECTED")

    def test_pessimistic_opportunity_without_fill_requires_exact_replay_not_simulator_relaxation(self) -> None:
        out = self._run([self._order(queue=5), self._state("o1", "CANCELLED", 105_000)],
                        [(102, 102_100, "t1", "SELL", .50, 10.0, "tr1")])
        self.assertEqual(out["funnel"]["fill_opportunity_pessimistic"], 1)
        self.assertEqual(out["root_cause"], "PESSIMISTIC_OPPORTUNITY_WITHOUT_FILL")
        self.assertEqual(out["simulator_bug_suspected"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(out["next_experiment"], "deterministic_exact_ws_replay")
        self.assertFalse(out["quality"]["simulator_relaxation_performed"])

    def test_cancel_effective_censors_later_flow(self) -> None:
        out = self._run([self._order(queue=5), self._state("o1", "CANCEL_PENDING", 101_500), self._state("o1", "CANCELLED", 102_500)],
                        [(103, 103_100, "t1", "SELL", .50, 20.0, "late")])
        self.assertEqual(out["funnel"]["trade_reachable"], 0)
        self.assertEqual(out["funnel"]["cancelled_before_flow"], 1)

    def test_actual_full_fill_is_counted_separately_from_counterfactuals(self) -> None:
        out = self._run([self._order(queue=5), self._fill("o1", 2.0), self._state("o1", "FILLED", 103_100)],
                        [(102, 102_100, "t1", "SELL", .50, 10.0, "tr1")])
        self.assertEqual(out["funnel"]["full_fills"], 1)
        self.assertEqual(out["orders"][0]["fillability_classification"], "FILLED")

    def test_market_and_action_aggregates_preserve_order_counts(self) -> None:
        rows = [self._order("o1", queue=5), self._state("o1", "CANCELLED", 105_000),
                self._order("o2", queue=8, exchange_ms=106_000, decision_ms=106_060), self._state("o2", "CANCELLED", 110_000)]
        out = self._run(rows, [(102, 102_100, "t1", "SELL", .50, 1.0, "tr1")], now_ms=111_000)
        self.assertEqual(sum(row["orders"] for row in out["actions"]), 2)
        self.assertEqual(sum(row["orders"] for row in out["markets"]), 2)


if __name__ == "__main__":
    unittest.main()
