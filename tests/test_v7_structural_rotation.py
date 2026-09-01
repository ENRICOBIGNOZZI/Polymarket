import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_graph_rv_intents import parse_book, rotating_events, structural_scan_budget
from v7_structural_rotation import rotating_window


def test_canonical_structural_cursor_covers_all_events_without_permanent_first_n_selection():
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


def test_graph_research_book_requires_causal_exchange_clock():
    receive_ms = 1_800_000_000_000
    raw = {
        "asset_id": "token-1",
        "bids": [{"price": "0.49", "size": "10"}],
        "asks": [{"price": "0.51", "size": "10"}],
        "hash": "snapshot-1",
    }
    assert parse_book(raw, receive_ms) is None
    raw["timestamp"] = receive_ms - 1000
    assert parse_book(raw, receive_ms) is not None
    raw["timestamp"] = receive_ms + 1000
    assert parse_book(raw, receive_ms) is None
