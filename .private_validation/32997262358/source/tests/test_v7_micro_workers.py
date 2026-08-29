#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_maker_uses_fill_conditioned_toxicity_objective_and_dual_clock():
    text = (ROOT / "scripts/v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "maker_fill_conditioned_ev" in text
    assert "toxicity_score" in text
    assert "created_received_ms" in text
    assert "created_event_ms" in text
    assert "received_ms" in text
    assert "broker_owned_tokens" in text


def test_taker_uses_complete_round_trip_contract():
    text = (ROOT / "scripts/v7_micro_taker_worker.py").read_text(encoding="utf-8")
    assert "complete_round_trip_executable_ev" in text
    assert "expected_exit_price" in text
    assert "uncertainty_z" in text
    assert "adverse_markout_bps" in text
    assert "capital_cost_bps_per_hour" in text


if __name__ == "__main__":
    test_maker_uses_fill_conditioned_toxicity_objective_and_dual_clock()
    test_taker_uses_complete_round_trip_contract()
    print("ok 2 v7 micro worker tests")
