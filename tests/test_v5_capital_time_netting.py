#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "research_v5_capital_time_netting.py"
SPEC = importlib.util.spec_from_file_location("capital_time_netting", MODULE_PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)

Opportunity = MOD.Opportunity
evaluate = MOD.evaluate
incumbent_selection = MOD.incumbent_selection
select_challenger = MOD.select_challenger


def opp(
    ts: int,
    strategy: str,
    market: str,
    *,
    edge: float,
    hours: float,
    realized: float,
    incumbent: bool = False,
    cost: float = 0.001,
    notional: float = 50.0,
    background: float = 0.0,
) -> Opportunity:
    return Opportunity(
        ts,
        strategy,
        market,
        "YES",
        notional,
        hours,
        edge,
        cost,
        realized,
        incumbent,
        background,
    )


class CapitalTimeNettingTest(unittest.TestCase):
    def test_incumbent_is_exact_replay_flag_not_guessed_ranking(self) -> None:
        rows = [
            opp(100, "micro", "a", edge=0.001, hours=1.0, realized=1.0, incumbent=True),
            opp(100, "graph", "b", edge=0.100, hours=1.0, realized=1.0, incumbent=False),
        ]
        self.assertEqual(incumbent_selection(rows), {0})

    def test_two_x_cost_gate_is_fail_closed(self) -> None:
        rows = [
            opp(100, "micro", "a", edge=0.0015, hours=1.0, realized=1.0, incumbent=True, cost=0.002),
            opp(100, "graph", "b", edge=0.0030, hours=2.0, realized=1.0, cost=0.001),
        ]
        selected = select_challenger(rows, global_capital_budget=50.0, window_seconds=60)
        self.assertEqual(selected, {1})
        self.assertLess(rows[0].robust_edge(), 0.0)

    def test_capital_time_prefers_faster_equal_edge_with_same_incumbent_budget(self) -> None:
        rows = [
            opp(120, "pca", "slow", edge=0.01, hours=24.0, realized=1.0, incumbent=True),
            opp(120, "graph", "fast", edge=0.01, hours=2.0, realized=1.0),
        ]
        selected = select_challenger(rows, global_capital_budget=50.0, window_seconds=60)
        self.assertEqual(selected, {1})

    def test_selection_does_not_use_future_realized_pnl(self) -> None:
        base = [
            opp(120, "pca", "a", edge=0.01, hours=8.0, realized=-100.0, incumbent=True),
            opp(120, "graph", "b", edge=0.009, hours=1.0, realized=100.0),
        ]
        flipped = [
            opp(120, "pca", "a", edge=0.01, hours=8.0, realized=1000.0, incumbent=True),
            opp(120, "graph", "b", edge=0.009, hours=1.0, realized=-1000.0),
        ]
        a = select_challenger(base, global_capital_budget=50.0, window_seconds=60)
        b = select_challenger(flipped, global_capital_budget=50.0, window_seconds=60)
        self.assertEqual(a, b)

    def test_no_incumbent_capital_means_no_challenger_activity(self) -> None:
        rows = [opp(120, "graph", "fast", edge=0.10, hours=0.5, realized=100.0, incumbent=False)]
        selected = select_challenger(rows, global_capital_budget=50.0, window_seconds=60)
        self.assertEqual(selected, set())

    def test_background_occupancy_does_not_create_challenger_budget(self) -> None:
        rows = [
            opp(
                120,
                "graph",
                "fast",
                edge=0.10,
                hours=0.5,
                realized=100.0,
                incumbent=False,
                background=40.0,
            )
        ]
        selected = select_challenger(rows, global_capital_budget=50.0, window_seconds=60)
        self.assertEqual(selected, set())

    def test_challenger_cannot_use_more_than_incumbent_capital(self) -> None:
        rows = [
            opp(120, "pca", "inc", edge=0.01, hours=10.0, realized=0.0, incumbent=True, notional=40.0),
            opp(120, "micro", "a", edge=0.02, hours=1.0, realized=1.0, notional=25.0),
            opp(120, "graph", "b", edge=0.018, hours=1.0, realized=1.0, notional=25.0),
        ]
        selected = select_challenger(rows, global_capital_budget=100.0, window_seconds=60)
        self.assertEqual(len(selected), 1)
        self.assertLessEqual(sum(rows[i].notional for i in selected), 40.0)

    def test_background_occupancy_is_included_in_global_cap_check(self) -> None:
        rows = [
            opp(
                120,
                "pca",
                "inc",
                edge=0.01,
                hours=10.0,
                realized=0.0,
                incumbent=True,
                notional=40.0,
                background=20.0,
            ),
            opp(
                120,
                "graph",
                "alt",
                edge=0.02,
                hours=1.0,
                realized=1.0,
                notional=40.0,
                background=20.0,
            ),
        ]
        with self.assertRaises(ValueError):
            select_challenger(rows, global_capital_budget=50.0, window_seconds=60)

    def test_background_occupancy_must_be_consistent_within_window(self) -> None:
        rows = [
            opp(120, "pca", "inc", edge=0.01, hours=10.0, realized=0.0, incumbent=True, notional=20.0, background=10.0),
            opp(120, "graph", "alt", edge=0.02, hours=1.0, realized=1.0, notional=20.0, background=11.0),
        ]
        with self.assertRaises(ValueError):
            select_challenger(rows, global_capital_budget=50.0, window_seconds=60)

    def test_report_exposes_background_and_total_capital(self) -> None:
        rows = [
            opp(120, "pca", "inc", edge=0.01, hours=10.0, realized=0.0, incumbent=True, notional=20.0, background=10.0),
            opp(120, "graph", "alt", edge=0.02, hours=1.0, realized=1.0, notional=20.0, background=10.0),
        ]
        report = evaluate(rows, global_capital_budget=50.0, window_seconds=60, fold_seconds=1000)
        window = report["capital_windows"][0]
        self.assertEqual(window["background_reserved_capital"], 10.0)
        self.assertEqual(window["incumbent_total_capital"], 30.0)
        self.assertEqual(window["challenger_total_capital"], 30.0)

    def test_decision_windows_do_not_share_capital(self) -> None:
        rows = [
            opp(60, "micro", "inc1", edge=0.01, hours=5.0, realized=0.0, incumbent=True, notional=50.0),
            opp(60, "graph", "alt1", edge=0.02, hours=1.0, realized=1.0, notional=50.0),
            opp(121, "micro", "inc2", edge=0.01, hours=5.0, realized=0.0, incumbent=True, notional=50.0),
            opp(121, "graph", "alt2", edge=0.02, hours=1.0, realized=1.0, notional=50.0),
        ]
        selected = select_challenger(rows, global_capital_budget=50.0, window_seconds=60)
        self.assertEqual(selected, {1, 3})

    def test_report_requires_multiple_chronological_folds(self) -> None:
        rows = [
            opp(100, "micro", "a", edge=0.01, hours=1.0, realized=1.0, incumbent=True),
            opp(200, "graph", "b", edge=0.01, hours=1.0, realized=1.0),
        ]
        report = evaluate(rows, global_capital_budget=50.0, window_seconds=60, fold_seconds=1000)
        self.assertFalse(report["evidence_ready"])
        self.assertEqual(report["folds"], 1)

    def test_positive_incremental_pnl_across_stress_can_be_evidence_ready(self) -> None:
        rows = [
            opp(100, "pca", "slow1", edge=0.020, hours=20.0, realized=-1.0, incumbent=True, cost=0.001),
            opp(100, "micro", "fast1", edge=0.015, hours=1.0, realized=2.0, cost=0.001),
            opp(4000, "pca", "slow2", edge=0.020, hours=20.0, realized=-1.0, incumbent=True, cost=0.001),
            opp(4000, "micro", "fast2", edge=0.015, hours=1.0, realized=2.0, cost=0.001),
        ]
        report = evaluate(rows, global_capital_budget=50.0, window_seconds=60, fold_seconds=3000)
        self.assertTrue(report["evidence_ready"])
        self.assertTrue(report["capital_parity_ok"])
        self.assertEqual(report["folds"], 2)
        for stress in ("1.0", "1.5", "2.0"):
            self.assertGreater(report["stress"][stress]["incremental_pnl"], 0.0)

    def test_incumbent_replay_over_global_cap_fails_closed(self) -> None:
        rows = [opp(100, "micro", "a", edge=0.01, hours=1.0, realized=1.0, incumbent=True, notional=60.0)]
        with self.assertRaises(ValueError):
            evaluate(rows, global_capital_budget=50.0)

    def test_empty_input_is_more_evidence_required(self) -> None:
        report = evaluate([], global_capital_budget=50.0)
        self.assertEqual(report["status"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(report["evidence_ready"])


if __name__ == "__main__":
    unittest.main()
