from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import v7_global_portfolio_coordinator as coordinator  # noqa: E402
from test_v7_opportunity import envelope  # noqa: E402
from test_v7_external_cancel_opportunity_bridge import NOW, setup_case  # noqa: E402
from v7_opportunity import OpportunityEnvelope, coordinate  # noqa: E402


def cancel_envelope() -> dict:
    value = envelope(
        action="CANCEL", component="professional_maker", ev=0.0, key="external-cancel",
        authority="PAPER_EXPLORATION", research_only=False,
    )
    value["side"] = "NONE"
    value["inventory_delta"] = 0.0
    value["portfolio_exposure_delta"] = 0.0
    value["execution_plan"]["unwind_plan"] = "CANCEL_ONLY"
    value["reasons"] = [
        "RESEARCH_CANCEL_RULE_MATCH",
        "LIVE_RECEIVE_TIME_TRIGGER_ACTIVE",
        "STALE_BUY_OUTCOME_YES",
    ]
    return OpportunityEnvelope.parse(value).raw


def test_cancel_preempts_and_publishes_receipt_gated_executor_intent() -> None:
    cancel = cancel_envelope()
    decision = coordinate([cancel], now_ns=150, new_risk_authorized=False, paper_exploration_authorized=True)
    assert decision["action"] == "CANCEL"
    assert decision["new_risk_authorized"] is False
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator._publish_cancel_authorization(root, decision, [cancel])
        rows = list((root / "micro_maker/authorized_cancel").glob("*.json"))
        assert len(rows) == 1
        value = json.loads(rows[0].read_text())
        assert value["schema"] == "polymarket_v7_authorized_cancel_intent_v1"
        assert value["owner"] == "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        assert value["execution_authority"] == "SIMULATED_PAPER_CANCEL_ONLY"
        assert value["opportunity_envelope"]["action"] == "CANCEL"
        assert value["real_order_submission"] is False
        assert value["coordinator_authorized_wall_ns"] > 0
        assert value["signal_trigger_wall_ns"] > 0
        assert value["signal_age_ns_at_authorization"] >= 0


def test_non_frozen_cancel_is_never_published_to_executor() -> None:
    cancel = cancel_envelope()
    cancel["reasons"] = ["UNRELATED_RISK_CANCEL"]
    decision = coordinate([cancel], now_ns=150)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        coordinator._publish_cancel_authorization(root, decision, [cancel])
        assert not (root / "micro_maker/authorized_cancel").exists()


def test_fast_cancel_lane_publishes_without_full_portfolio_cut() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        setup_case(root)
        status = coordinator.process_fast_cancel(root, now_ns=NOW)
        assert status["state"] == "CANCEL_AUTHORIZED"
        assert status["cancel_opportunities"] == 1
        assert status["signal_age_ns"] == 10_000_000
        rows = list((root / "micro_maker/authorized_cancel").glob("*.json"))
        assert len(rows) == 1
        value = json.loads(rows[0].read_text())
        assert value["fast_cancel_path"] is True
        assert value["signal_trigger_wall_ns"] == NOW - 10_000_000
        assert value["decision"]["fast_cancel_path"] is True
        assert (root / "opportunities/fast_cancel_decisions.jsonl").exists()


if __name__ == "__main__":
    test_cancel_preempts_and_publishes_receipt_gated_executor_intent()
    test_non_frozen_cancel_is_never_published_to_executor()
    test_fast_cancel_lane_publishes_without_full_portfolio_cut()
