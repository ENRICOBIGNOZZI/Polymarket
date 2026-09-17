from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_opportunity import OpportunityEnvelope, OpportunityError, coordinate  # noqa: E402


def envelope(*, action="TAKE", component="crypto_informed_taker", ev=1.0, key="a", asset="BTC", horizon="M5", authority="SHADOW", research_only=False) -> dict:
    legs = [{
        "leg_id": "leg-1", "market_id": "market-1", "contract_id": "contract-1",
        "token_id": "token-1", "side": "BUY", "target_quantity": 10.0,
        "limit_price": 0.50, "fee_authority": "AUTHORITATIVE",
    }]
    return {
        "schema": "polymarket_v7_opportunity_envelope_v1",
        "version": 1,
        "model_sha": "a" * 40,
        "config_hash": "b" * 64,
        "policy_hash": "c" * 64,
        "run_id": "run-1",
        "source_snapshot_identity": "cut-7",
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "component_provenance": [component],
        "market_id": "market-1",
        "event_id": "event-1",
        "contract_id": "contract-1",
        "mapping_identity": "mapping-1",
        "crypto_context": {
            "asset": asset, "horizon": horizon,
            "contract_family": f"{asset}_USD_UPDOWN_{horizon}",
            "settlement_semantic_hash": "d" * 64,
            "authority": authority, "research_only": research_only,
        },
        "action": action,
        "side": "BUY" if action not in {"CANCEL", "WITHDRAW", "NOTHING"} else "NONE",
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
        "execution_plan": {
            "atomic_unit_id": f"atomic-{key}",
            "execution_style": "SINGLE_LEG",
            "legs": legs,
            "partial_fill_plan": "CANCEL_REMAINDER",
            "timeout_ms": 100,
            "unwind_plan": "NONE",
        },
        "inventory_delta": 10.0,
        "portfolio_exposure_delta": 5.0,
        "settlement": {"definition": "verified rules", "source": "contract registry", "verified": True},
        "eligible": True,
        "reasons": ["EVIDENCE_COMPLETE"],
        "deterministic_replay_key": key,
        "expires_at_ns": 200,
    }


def multi_forward_envelope(*, asset: str = "ETH", horizon: str = "M5", key: str = "multi-forward") -> dict:
    value = envelope(action="TAKE", ev=0.0, key=key, asset=asset, horizon=horizon,
                     authority="PAPER_EXPLORATION", research_only=False)
    value["uncertainty"] = {"lower_bound": 0.0, "upper_bound": 1.0, "status": "IMMATURE"}
    value["calibration_status"] = "NOT_APPLICABLE"
    value["multi_crypto_forward"] = {
        "mode": "PAPER_MULTI_CRYPTO_FORWARD", "experiment_id": f"{asset}-{horizon}-cohort",
        "protocol_hash": "f" * 64, "feature_schema_hash": "e" * 64,
        "model_hash": "1" * 64, "fill_model_hash": "2" * 64,
        "cost_model_hash": "3" * 64,
        "settlement_semantic_hash": value["crypto_context"]["settlement_semantic_hash"],
        "latency_profile_id": value["latency"]["profile_id"],
        "asset": asset, "horizon": horizon, "research_only": True,
        "automatic_promotion": False, "one_entry_per_market": True,
        "hold_to_settlement": True, "entry_uses_absolute_fair": False,
        "probability_source": "POLYMARKET_PRIOR_ONLY",
    }
    return value


def test_complete_envelope_parses() -> None:
    parsed = OpportunityEnvelope.parse(envelope())
    assert parsed.engine_id == "CRYPTO_SETTLEMENT_ENGINE"
    assert parsed.expected_wealth_change == 1.0


def test_zero_arrival_latency_is_valid_and_not_missing() -> None:
    value = envelope()
    value["latency"]["arrival_ns"] = 0
    parsed = OpportunityEnvelope.parse(value)
    assert parsed.raw["latency"]["arrival_ns"] == 0


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
    assert decision["engine_id"] == "CRYPTO_SETTLEMENT_ENGINE"
    assert decision["new_risk_authorized"] is False


def test_coordinator_compares_crypto_actions_on_one_objective() -> None:
    take = envelope(action="TAKE", component="crypto_informed_taker", ev=1.0, key="take")
    make = envelope(action="MAKE", component="professional_maker", ev=2.0, key="make")
    decision = coordinate([take, make], now_ns=150, new_risk_authorized=True)
    assert decision["action"] == "MAKE"
    assert decision["engine_id"] == "CRYPTO_SETTLEMENT_ENGINE"
    assert decision["selected_replay_key"] == "make"


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


def test_crypto_context_is_mandatory_and_zero_authority_cannot_add_risk() -> None:
    missing = envelope(action="NOTHING")
    missing["crypto_context"] = None
    try:
        OpportunityEnvelope.parse(missing)
    except OpportunityError as exc:
        assert str(exc) == "crypto_context"
    else:
        raise AssertionError("crypto opportunity without context accepted")
    zero = envelope(asset="ETH", authority="SHADOW_ZERO_AUTHORITY", research_only=True)
    try:
        OpportunityEnvelope.parse(zero)
    except OpportunityError as exc:
        assert str(exc) == "crypto_context_zero_authority"
    else:
        raise AssertionError("non-BTC research context received new-risk authority")
    zero["action"] = "NOTHING"
    zero["side"] = "NONE"
    assert OpportunityEnvelope.parse(zero).raw["crypto_context"]["asset"] == "ETH"


def test_doge_and_bnb_are_typed_crypto_contexts_but_zero_authority_still_blocks_risk() -> None:
    for asset in ("DOGE", "BNB"):
        value = envelope(asset=asset, authority="SHADOW_ZERO_AUTHORITY", research_only=True)
        try:
            OpportunityEnvelope.parse(value)
        except OpportunityError as exc:
            assert str(exc) == "crypto_context_zero_authority"
        else:
            raise AssertionError(f"{asset} zero-authority context added risk")


def test_all_crypto_contexts_compete_in_one_global_cut() -> None:
    eth = envelope(asset="ETH", authority="PAPER", ev=2.0, key="eth")
    sol = envelope(asset="SOL", authority="PAPER", ev=3.0, key="sol")
    decision = coordinate([eth, sol], now_ns=150, new_risk_authorized=True)
    assert decision["selected_replay_key"] == "sol"
    assert decision["crypto_context"]["asset"] == "SOL"


def test_frozen_forward_take_is_paper_authorized_without_absolute_fair_ev() -> None:
    value = envelope(ev=0.0, key="lead-lag-forward", authority="PAPER_EXPLORATION")
    value["uncertainty"] = {"lower_bound": 0.0, "upper_bound": 1.0, "status": "IMMATURE"}
    value["calibration_status"] = "NOT_APPLICABLE"
    value["latency"]["profile_valid"] = False
    value["fair_value"] = {"lower": 0.0, "point": 0.55, "upper": 1.0}
    value["forward_test"] = {
        "mode": "PAPER_FORWARD_TEST", "strategy_id": "LEAD_LAG_TAKER_V1",
        "protocol_hash": "f" * 64, "research_only": True,
        "automatic_promotion": False, "one_entry_per_market": True,
        "hold_to_settlement": True, "entry_uses_absolute_fair": False,
        "probability_source": "POLYMARKET_PRIOR_ONLY",
    }
    parsed = OpportunityEnvelope.parse(value)
    assert parsed.is_forward_test is True
    decision = coordinate([value], now_ns=150, new_risk_authorized=False,
                          paper_exploration_authorized=True)
    assert decision["action"] == "TAKE"
    assert decision["paper_forward_test_authorized"] is True
    assert decision["new_risk_authorized"] is False
    assert decision["reasons"] == ["PAPER_FORWARD_TEST_FROZEN_PROTOCOL"]


def test_multi_crypto_forward_contract_is_shared_across_six_assets_and_two_horizons() -> None:
    for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
        for horizon in ("M5", "M15"):
            value = multi_forward_envelope(asset=asset, horizon=horizon, key=f"{asset}-{horizon}")
            parsed = OpportunityEnvelope.parse(value)
            assert parsed.is_multi_crypto_forward is True
            decision = coordinate([value], now_ns=150, new_risk_authorized=False,
                                  paper_exploration_authorized=True)
            assert decision["action"] == "TAKE"
            assert decision["paper_multi_crypto_forward_authorized"] is True
            assert decision["new_risk_authorized"] is False
            assert decision["real_order_submission"] is False
            assert decision["crypto_context"]["asset"] == asset
            assert decision["crypto_context"]["horizon"] == horizon


def test_multi_crypto_forward_requires_full_frozen_lineage_and_valid_latency() -> None:
    for mutation in ("settlement", "latency", "protocol"):
        value = multi_forward_envelope()
        if mutation == "settlement":
            value["multi_crypto_forward"]["settlement_semantic_hash"] = "9" * 64
        elif mutation == "latency":
            value["latency"]["profile_valid"] = False
        else:
            value["multi_crypto_forward"]["protocol_hash"] = "not-a-hash"
        try:
            OpportunityEnvelope.parse(value)
            assert False, mutation
        except OpportunityError:
            pass


def test_non_btc_paper_exploration_without_multi_forward_packet_is_rejected() -> None:
    value = envelope(ev=0.0, asset="ETH", horizon="M5", authority="PAPER_EXPLORATION")
    value["uncertainty"] = {"lower_bound": 0.0, "upper_bound": 1.0, "status": "IMMATURE"}
    value["calibration_status"] = "NOT_APPLICABLE"
    try:
        OpportunityEnvelope.parse(value)
        assert False
    except OpportunityError as exc:
        assert "paper_exploration_evidence_incomplete" in str(exc)


def test_multi_crypto_forward_does_not_authorize_itself_without_paper_gate() -> None:
    value = multi_forward_envelope(asset="SOL", horizon="M5")
    decision = coordinate([value], now_ns=150, new_risk_authorized=False,
                          paper_exploration_authorized=False)
    assert decision["action"] == "NOTHING"
    assert decision["paper_multi_crypto_forward_authorized"] is False
    assert decision["new_risk_authorized"] is False


if __name__ == "__main__":
    test_complete_envelope_parses()
    test_unauthoritative_rebate_fails_closed()
    test_missing_latency_allows_cancel_but_not_new_risk()
    test_risk_cancel_preempts_positive_alpha()
    test_coordinator_compares_crypto_actions_on_one_objective()
    test_coordinator_defaults_to_nothing_without_new_risk_authority()
    test_invalid_or_duplicate_envelope_fails_the_whole_cut_closed()
    test_crypto_context_is_mandatory_and_zero_authority_cannot_add_risk()
    test_doge_and_bnb_are_typed_crypto_contexts_but_zero_authority_still_blocks_risk()
    test_all_crypto_contexts_compete_in_one_global_cut()
    test_frozen_forward_take_is_paper_authorized_without_absolute_fair_ev()
    test_multi_crypto_forward_contract_is_shared_across_six_assets_and_two_horizons()
    test_multi_crypto_forward_requires_full_frozen_lineage_and_valid_latency()
    test_non_btc_paper_exploration_without_multi_forward_packet_is_rejected()
    test_multi_crypto_forward_does_not_authorize_itself_without_paper_gate()
