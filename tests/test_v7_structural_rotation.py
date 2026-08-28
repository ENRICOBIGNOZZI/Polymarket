import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_graph_rv_intents import rotating_events, structural_scan_budget
from v7_hard_arb_guard import rotating_window


def test_hard_arb_cursor_covers_all_events_without_permanent_first_n_selection():
    values = [f"e{index}" for index in range(7)]
    cursor = 0
    observed = []
    for _ in range(3):
        selected, cursor = rotating_window(values, cursor, 3)
        observed.extend(selected)
    assert set(observed) == set(values)
    assert observed[:3] == ["e0", "e1", "e2"]


def test_graph_rotation_and_budget_are_resource_based():
    cfg = {"v7": {"adaptive_universe_policy": "config/v7_adaptive_universe.json"}}
    budget = structural_scan_budget(cfg)
    assert budget == 5000 // 60
    values = [f"e{index}" for index in range(budget + 5)]
    first, cursor = rotating_events(values, now=15, budget=budget, cycle_seconds=15)
    second, next_cursor = rotating_events(values, now=30, budget=budget, cycle_seconds=15)
    assert len(first) == len(second) == budget
    assert cursor != next_cursor
    assert set(first) | set(second) == set(values)
