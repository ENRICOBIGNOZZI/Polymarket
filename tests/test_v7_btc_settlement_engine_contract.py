from __future__ import annotations

import json
import copy
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_btc_settlement_engine_contract import (  # noqa: E402
    freeze, validate_config, validate_live_scope, validate_registry_authority,
    validate_structural_config,
)


SHA = "a" * 40


def config() -> dict:
    return json.loads((ROOT / "config/v7_btc_settlement_engine.json").read_text())


def registry() -> dict:
    return json.loads((ROOT / "config/v7_strategy_registry.json").read_text())


def structural() -> dict:
    return json.loads((ROOT / "config/v7_structural_arb_engine.json").read_text())


def live_scope() -> dict:
    return json.loads((ROOT / "config/v7_live_model_scope.json").read_text())


def latency() -> dict:
    return {
        "schema": "polymarket_v7_empirical_latency_profile_v1",
        "profile_version": 7,
        "exact_code_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "configured_constants_used": False,
        "segments": {
            name: {
                "samples": 200,
                "p50_ms": value * 0.40,
                "p90_ms": value * 0.70,
                "p95_ms": value * 0.85,
                "p99_ms": value,
                "p99_9_ms": value * 1.20,
                "max_ms": value * 1.50,
            }
            for name, value in {
                "taker_arrival": 75,
                "maker_place_ack": 50,
                "maker_cancel_ack": 100,
                "private_ws_confirmation": 125,
            }.items()
        },
    }


def maker() -> dict:
    return {
        "schema": "polymarket_v7_maker_execution_evidence_v1",
        "model_version": 8,
        "exact_code_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "independent_orders": 200,
        "reach_probability_lower": 0.30,
        "fill_given_reach_probability_lower": 0.40,
        "adverse_markout_upper_per_share": 0.003,
        "fill_conditioned_markout": True,
        "mature": True,
    }


def test_config_and_horizon_separation() -> None:
    value = config()
    validate_config(value)
    validate_structural_config(structural())
    validate_registry_authority(value, structural(), registry())
    validate_live_scope(value, structural(), live_scope())
    scopes = {row["model_scope"] for row in value["horizons"]}
    assert len(scopes) == 3
    five = freeze(
        value, code_sha=SHA, horizon_seconds=300, structural_config=structural(), registry=registry(),
        live_scope=live_scope(),
        latency_profile=latency(), maker_evidence=maker(),
    )
    assert five["new_risk_authorized"] is True
    assert five["horizon_policy"]["maker_enabled"] is True
    assert five["horizon_policy"]["taker_enabled"] is True
    assert five["latency"]["taker_arrival_p99_seconds"] == 0.075
    assert five["maker_execution"]["reach_probability_lower"] == 0.30

    research = freeze(
        value, code_sha=SHA, horizon_seconds=14_400, structural_config=structural(), registry=registry(),
        live_scope=live_scope(),
        latency_profile=latency(), maker_evidence=maker(),
    )
    assert research["new_risk_authorized"] is False
    assert research["horizon_policy"]["research_only"] is True
    assert research["horizon_policy"]["maker_enabled"] is False
    assert research["horizon_policy"]["taker_enabled"] is False


def test_missing_execution_truth_fails_closed_but_preserves_cancel_path() -> None:
    snapshot = freeze(
        config(), code_sha=SHA, horizon_seconds=300, structural_config=structural(), registry=registry(),
        live_scope=live_scope(),
    )
    assert snapshot["new_risk_authorized"] is False
    assert snapshot["cancel_path_independent"] is True
    assert snapshot["latency"]["valid"] is False
    assert snapshot["maker_execution"]["valid"] is False
    assert "EMPIRICAL_LATENCY_PROFILE_MISSING" in snapshot["blockers"]


def test_maker_and_taker_evidence_gates_are_independent() -> None:
    snapshot = freeze(
        config(), code_sha=SHA, horizon_seconds=900, structural_config=structural(), registry=registry(),
        live_scope=live_scope(),
        latency_profile=latency(), maker_evidence={},
    )
    assert snapshot["horizon_policy"]["taker_enabled"] is True
    assert snapshot["horizon_policy"]["maker_enabled"] is False
    assert snapshot["new_risk_authorized"] is True


def test_maker_cannot_regain_independent_economic_authority() -> None:
    value = copy.deepcopy(config())
    value["maker_economics"]["independent_capital_oms_inventory_ledger_authority"] = True
    try:
        validate_config(value)
    except ValueError as exc:
        assert str(exc) == "maker_component_boundary"
    else:
        raise AssertionError("maker independent authority accepted")


def test_external_updates_retain_cancel_and_reprice_preemption() -> None:
    value = copy.deepcopy(config())
    value["external_update_policy"]["oracle_or_external_update_can_cancel_without_polymarket_book_event"] = False
    try:
        validate_config(value)
    except ValueError as exc:
        assert str(exc) == "external_update_risk_preemption"
    else:
        raise AssertionError("external-update cancel preemption removed")


def test_structural_engine_has_one_atomic_bundle_and_shared_owners() -> None:
    value = structural()
    validate_structural_config(value)
    broken = copy.deepcopy(value)
    broken["one_capital_reservation_per_bundle"] = False
    try:
        validate_structural_config(broken)
    except ValueError as exc:
        assert str(exc) == "structural_engine_contract"
    else:
        raise AssertionError("duplicate structural reservation surface accepted")


if __name__ == "__main__":
    test_config_and_horizon_separation()
    test_missing_execution_truth_fails_closed_but_preserves_cancel_path()
    test_maker_and_taker_evidence_gates_are_independent()
    test_maker_cannot_regain_independent_economic_authority()
    test_external_updates_retain_cancel_and_reprice_preemption()
    test_structural_engine_has_one_atomic_bundle_and_shared_owners()
