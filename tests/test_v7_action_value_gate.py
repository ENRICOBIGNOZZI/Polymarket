from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_action_value_gate as m


def candidate(action, value, **overrides):
    row = {
        "action": action,
        "score_semantics": "JOINT_ACTION_VALUE",
        "units": "USD",
        "conservative_action_value": value,
        "uncertainty": 0.1,
        "fill_conditioned": True,
        "action_transport_validated": True,
        "oos_validated": True,
        "model_id": action.lower() + "-model",
    }
    row.update(overrides)
    return row


def test_refuses_settlement_edge_and_maker_ev_as_if_comparable():
    result = m.compare([
        candidate("MAKE", 0.4),
        candidate("TAKE", 0.8,
                  score_semantics="SETTLEMENT_EDGE",
                  fill_conditioned=False,
                  action_transport_validated=False),
    ])
    assert result["state"] == "NOT_COMPARABLE"
    assert result["selected_action"] is None
    assert "TAKE:NOT_JOINT_ACTION_VALUE" in result["blockers"]
    assert result["factorized_fill_times_edge_forbidden"] is True


def test_selects_highest_positive_joint_action_value_only_in_research():
    result = m.compare([candidate("MAKE", 0.2), candidate("TAKE", 0.5)])
    assert result["state"] == "COMPARABLE_RESEARCH_ONLY"
    assert result["selected_action"] == "TAKE"
    assert result["execution_authority"] is False
    assert result["automatic_promotion"] is False


def test_nothing_wins_when_all_new_risk_values_are_nonpositive():
    result = m.compare([candidate("MAKE", -0.2), candidate("TAKE", 0.0)])
    assert result["selected_action"] == "NOTHING"


def test_missing_oos_validation_fails_closed():
    result = m.compare([candidate("MAKE", 0.2, oos_validated=False)])
    assert result["state"] == "NOT_COMPARABLE"
    assert "MAKE:OOS_NOT_VALIDATED" in result["blockers"]
