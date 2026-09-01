from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_opportunity import OpportunityEnvelope, OpportunityError, coordinate  # noqa: E402


def envelope(*, engine="BTC_SETTLEMENT_ENGINE", action="TAKE", component="crypto_informed_taker", ev=1.0, key="a") -> dict:
    return {
        "schema": "polymarket_v7_opportunity_envelope_v1",
        "version": 1,
        "model_sha": "a" * 40,
        "config_hash": "b" * 64,
        "policy_hash": "c" * 64,
        "run_id": "run-1",
        "source_snapshot_identity": "cut-7",
        "engine_id": engine,
        "component_provenance": [component],
        "market_id": "market-1",
        "event_id": "event-1",
        "contract_id": "contract-1",
        "mapping_identity": "mapping-1",
        "action": action,
        "side": "BUY" if action not in {"ARB", "CANCEL", "NOTHING"} else ("MULTI" if action == "ARB" else "NONE"),
        "decision_receive_timestamp_ns": 100,
        "source_event_timestamps_ns": [80, 90],
        "fair_value": {"lower": 0.50, "point": 0.55, "upper": 0.60},
        "conservative_expected_wealth_change": ev,
        "cost_vector": {
            "fee": 0.01, "slippage": 0.01, "unwind_loss": 0.01,
            "capital_cost": 0.01, "latency_cost": 0.01,
            "adverse_markout": 0.01, "rebate": 0.0,
        },
        "cost_authority": {
            "fee": "AUTHORITATIVE", "slippage": "CONSERVATIVE_BOUND",
            "unwind_loss": "CONSERVATIVE_BOUND", "capital_cost": "AUTHORITATIVE",
            "latency_cost": "CONSERVATIVE_BOUND", "adverse_markout": "CONSERVATIVE_BOUND",
            "rebate": "CONSERVATIVE_ZERO",
        },
        "uncertainty": {"lower_bound": 0.1, "upper_bound": 0.2, "status": "MATURE"},
        "calibration_status": "MATURE",
        "latency": {"profile_id": "latency-1", "profile_valid": True, "economic_percentile": "p99", "arrival_ns": 10},
        "capacity": {"executable_size": 10.0, "depth_provenance": "full-l2-cut-7"},
        "inventory_delta": 10.0,
        "portfolio_exposure_delta": 5.0,
        "settlement": {"definition": "verified rules", "source": "contract registry", "verified": True},
        "eligible": True,
        "reasons": ["EVIDENCE_COMPLETE"],
        "deterministic_replay_key": key,
        "expires_at_ns": 200,
    }


def test_complete_envelope_parses() -> None:
    parsed = OpportunityEnvelope.parse(envelope())
    assert parsed.engine_id == "BTC_SETTLEMENT_ENGINE"
    assert parsed.expected_wealth_change == 1.0


def test_unauthoritative_rebate_fails_closed() -> None:
    value = envelope()
    value["cost_vector"]["rebate"] = 0.01
    try:
        OpportunityEnvelope.parse(value)
    except OpportunityError as exc:
        assert str(exc) == "unauthoritative_rebate_nonzero"
    else:
        raise AssertionError("unauthoritative rebate accepted")


def test_missing_latency_allows_cancel_but_not_new_risk() -> None:
    value = envelope()
    value["latency"]["profile_valid"] = False
    try:
        OpportunityEnvelope.parse(value)
    except OpportunityError as exc:
        assert str(exc) == "new_risk_evidence_incomplete"
    else:
        raise AssertionError("new risk accepted without latency")
    value["action"] = "CANCEL"
    value["side"] = "NONE"
    assert OpportunityEnvelope.parse(value).action == "CANCEL"


def test_risk_cancel_preempts_positive_alpha() -> None:
    cancel = envelope(action="CANCEL", component="professional_maker", key="cancel")
    cancel["side"] = "NONE"
    decision = coordinate([envelope(ev=10.0), cancel], now_ns=150, new_risk_authorized=True)
    assert decision["action"] == "CANCEL"
    assert decision["engine_id"] == "BTC_SETTLEMENT_ENGINE"
    assert decision["new_risk_authorized"] is False


def test_coordinator_compares_engines_on_one_objective() -> None:
    structural = envelope(
        engine="STRUCTURAL_ARB_ENGINE", action="ARB", component="hard_arb", ev=2.0, key="structural",
    )
    decision = coordinate([envelope(ev=1.0, key="btc"), structural], now_ns=150, new_risk_authorized=True)
    assert decision["action"] == "ARB"
    assert decision["engine_id"] == "STRUCTURAL_ARB_ENGINE"
    assert decision["selected_replay_key"] == "structural"


def test_coordinator_defaults_to_nothing_without_new_risk_authority() -> None:
    decision = coordinate([envelope()], now_ns=150)
    assert decision["action"] == "NOTHING"
    assert decision["reasons"] == ["NEW_RISK_NOT_AUTHORIZED"]


def test_invalid_or_duplicate_envelope_fails_the_whole_cut_closed() -> None:
    invalid = copy.deepcopy(envelope(key="bad"))
    invalid["source_event_timestamps_ns"] = [101]
    decision = coordinate([envelope(), invalid], now_ns=150, new_risk_authorized=True)
    assert decision["action"] == "NOTHING"
    assert decision["new_risk_authorized"] is False
    decision = coordinate([envelope(), envelope()], now_ns=150, new_risk_authorized=True)
    assert decision["action"] == "NOTHING"


if __name__ == "__main__":
    test_complete_envelope_parses()
    test_unauthoritative_rebate_fails_closed()
    test_missing_latency_allows_cancel_but_not_new_risk()
    test_risk_cancel_preempts_positive_alpha()
    test_coordinator_compares_engines_on_one_objective()
    test_coordinator_defaults_to_nothing_without_new_risk_authority()
    test_invalid_or_duplicate_envelope_fails_the_whole_cut_closed()
