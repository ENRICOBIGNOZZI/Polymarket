#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "research_v5_capital_time_netting.py"
SPEC = importlib.util.spec_from_file_location("capital_time_netting", MODULE_PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)

Opportunity = MOD.Opportunity
evaluate = MOD.evaluate
select_by_decision_window = MOD.select_by_decision_window


def opp(
    ts: int,
    strategy: str,
    market: str,
    *,
    edge: float,
    hours: float,
    realized: float,
    cost: float = 0.001,
    notional: float = 50.0,
) -> Opportunity:
    return Opportunity(ts, strategy, market, "YES", notional, hours, edge, cost, realized)


class CapitalTimeNettingTest(unittest.TestCase):
    def test_two_x_cost_gate_is_fail_closed(self) -> None:
        rows = [
            opp(100, "micro", "a", edge=0.0015, hours=1.0, realized=1.0, cost=0.002),
            opp(100, "graph", "b", edge=0.0030, hours=2.0, realized=1.0, cost=0.001),
        ]
        selected = select_by_decision_window(
            rows, capital_budget=100.0, window_seconds=60, score_kind="capital_time"
        )
        self.assertEqual(selected, {1})
        self.assertLess(rows[0].robust_edge(), 0.0)

    def test_capital_time_prefers_faster_equal_edge(self) -> None:
        rows = [
            opp(120, "pca", "slow", edge=0.01, hours=24.0, realized=1.0, notional=50.0),
            opp(120, "graph", "fast", edge=0.01, hours=2.0, realized=1.0, notional=50.0),
        ]
        selected = select_by_decision_window(
            rows, capital_budget=50.0, window_seconds=60, score_kind="capital_time"
        )
        self.assertEqual(selected, {1})

    def test_incumbent_prefers_larger_one_x_edge(self) -> None:
        rows = [
            opp(120, "pca", "slow", edge=0.012, hours=24.0, realized=1.0, notional=50.0),
            opp(120, "graph", "fast", edge=0.010, hours=2.0, realized=1.0, notional=50.0),
        ]
        selected = select_by_decision_window(
            rows, capital_budget=50.0, window_seconds=60, score_kind="incumbent"
        )
        self.assertEqual(selected, {0})

    def test_selection_does_not_use_future_realized_pnl(self) -> None:
        base = [
            opp(120, "pca", "a", edge=0.01, hours=8.0, realized=-100.0),
            opp(120, "graph", "b", edge=0.009, hours=1.0, realized=100.0),
        ]
        flipped = [
            opp(120, "pca", "a", edge=0.01, hours=8.0, realized=1000.0),
            opp(120, "graph", "b", edge=0.009, hours=1.0, realized=-1000.0),
        ]
        a = select_by_decision_window(
            base, capital_budget=50.0, window_seconds=60, score_kind="capital_time"
        )
        b = select_by_decision_window(
            flipped, capital_budget=50.0, window_seconds=60, score_kind="capital_time"
        )
        self.assertEqual(a, b)

    def test_decision_windows_do_not_compete_across_time(self) -> None:
        rows = [
            opp(60, "micro", "a", edge=0.02, hours=1.0, realized=1.0, notional=50.0),
            opp(121, "graph", "b", edge=0.02, hours=1.0, realized=1.0, notional=50.0),
        ]
        selected = select_by_decision_window(
            rows, capital_budget=50.0, window_seconds=60, score_kind="capital_time"
        )
        self.assertEqual(selected, {0, 1})

    def test_report_requires_multiple_chronological_folds(self) -> None:
        rows = [
            opp(100, "micro", "a", edge=0.01, hours=1.0, realized=1.0),
            opp(200, "graph", "b", edge=0.01, hours=1.0, realized=1.0),
        ]
        report = evaluate(rows, capital_budget=50.0, window_seconds=60, fold_seconds=1000)
        self.assertFalse(report["evidence_ready"])
        self.assertEqual(report["folds"], 1)

    def test_positive_incremental_pnl_across_stress_can_be_evidence_ready(self) -> None:
        rows = [
            opp(100, "pca", "slow1", edge=0.020, hours=20.0, realized=-1.0, cost=0.001),
            opp(100, "micro", "fast1", edge=0.015, hours=1.0, realized=2.0, cost=0.001),
            opp(4000, "pca", "slow2", edge=0.020, hours=20.0, realized=-1.0, cost=0.001),
            opp(4000, "micro", "fast2", edge=0.015, hours=1.0, realized=2.0, cost=0.001),
        ]
        report = evaluate(rows, capital_budget=50.0, window_seconds=60, fold_seconds=3000)
        self.assertTrue(report["evidence_ready"])
        self.assertEqual(report["folds"], 2)
        for stress in ("1.0", "1.5", "2.0"):
            self.assertGreater(report["stress"][stress]["incremental_pnl"], 0.0)

    def test_empty_input_is_more_evidence_required(self) -> None:
        report = evaluate([], capital_budget=50.0)
        self.assertEqual(report["status"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(report["evidence_ready"])


if __name__ == "__main__":
    unittest.main()
