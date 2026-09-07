from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_maker_opportunity_bridge as bridge  # noqa: E402
from v7_opportunity import OpportunityEnvelope  # noqa: E402


SHA = "a" * 40
SEMANTIC = "b" * 64


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def runtime() -> dict:
    return {
        "schema": "polymarket_v7_runtime_status_v3",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": SHA,
        "config_hash": "c" * 40,
        "policy_hash": "d" * 40,
        "run_id": "run-1",
    }


def selection() -> dict:
    quote = {
        "outcome": "YES",
        "token_id": "yes-token",
        "quote_side": "BUY",
        "best_bid": 0.50,
        "best_ask": 0.52,
        "tick_size": 0.01,
        "queue_ahead_shares": 5.0,
        "opposite_flow_shares_per_second": 12.0,
        "last_opposite_flow_age_ms": 15.0,
        "projected_join_fill_probability": 0.50,
        "projected_improve1_fill_probability": 0.65,
    }
    return {
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": SHA,
        "source": "adaptive_universe_recent_flow",
        "generated_at_ms": 1_000,
        "markets": [{
            "market_id": "market-1",
            "event_id": "event-1",
            "yes_token": "yes-token",
            "no_token": "no-token",
            "quote_opportunities": [quote],
            "authorized_execution_cells": [{
                "outcome": "YES",
                "token_id": "yes-token",
                "quote_side": "BUY",
                "action": "JOIN",
                "maximum_quote_shares": 5.0,
            }],
        }],
    }


def model(*, mature: bool = True) -> dict:
    return {
        "schema": "polymarket_v7_maker_execution_model_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": SHA,
        "model_state": "MATURE" if mature else "EVIDENCE_ACCUMULATING",
        "groups": {
            "JOIN:YES:BUY": {
                "mature": mature,
                "orders": 100 if mature else 10,
                "filled_orders": 50 if mature else 1,
                "fill_probability": 0.50,
                "adverse_markout_per_share": 0.001,
            },
            "GLOBAL": {
                "mature": mature,
                "orders": 100 if mature else 10,
                "filled_orders": 50 if mature else 1,
                "fill_probability": 0.50,
                "adverse_markout_per_share": 0.001,
            },
        },
    }


def fair_status() -> dict:
    return {
        "schema": "polymarket_v7_external_fair_status_v1",
        "state": "FULL_FAIR_SHADOW_OPERATIONAL",
        "code_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "market": {
            "market_id": "market-1",
            "event_id": "event-1",
            "yes_token": "yes-token",
            "no_token": "no-token",
        },
        "contract": {"verified": True, "rules_hash_recognized": True},
        "settlement_reference": {"valid": True},
        "oracle": {"healthy": True},
        "external": {
            "healthy": True,
            "dispersion_bps": 0.2,
            "realized_vol_fast": 2.0,
        },
        "fair": {
            "valid": True,
            "explicit_champion_applied": True,
            "lower": 0.65,
            "yes": 0.70,
            "upper": 0.75,
            "tte_seconds": 90.0,
            "probability_model_hash": "e" * 64,
            "settlement_semantic_hash": SEMANTIC,
        },
    }


def context() -> dict:
    return {
        "asset": "BTC",
        "horizon": "M5",
        "contract_family": "CRYPTO_UPDOWN",
        "settlement_semantic_hash": SEMANTIC,
        "authority": "SHADOW",
        "research_only": False,
    }


def setup_run(root: Path, *, mature: bool = True) -> None:
    write(root / "control/runtime_status.json", runtime())
    write(root / "micro_maker/reward_selection.json", selection())
    write(root / "micro_maker/execution_model.json", model(mature=mature))
    write(root / "external_fair/status.json", fair_status())


def test_mature_positive_cell_becomes_typed_make_opportunity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root)
        with mock.patch.object(bridge, "require_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert status["state"] == "OPERATIONAL"
        assert len(rows) == 1
        row = OpportunityEnvelope.parse(rows[0]).raw
        assert row["action"] == "MAKE"
        assert row["engine_id"] == "CRYPTO_SETTLEMENT_ENGINE"
        assert row["component_provenance"] == ["professional_maker"]
        assert row["execution_plan"]["legs"][0]["side"] == "BUY"
        assert row["conservative_expected_wealth_change"] > 0.0
        assert row["execution_alpha"]["fill_probability"]["lower"] > 0.0
        assert row["execution_alpha"]["evidence_status"] == "MATURE"
        assert row["crypto_context"]["authority"] == "PAPER_EXPLORATION"
        assert row["crypto_context"]["research_only"] is False


def test_immature_fill_model_does_not_manufacture_make_authority() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root, mature=False)
        with mock.patch.object(bridge, "require_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert rows == []
        assert status["state"] == "NO_EXECUTABLE_MAKE"
        assert status["rejected"]["INSUFFICIENT_CONSERVATIVE_EXECUTION_EVIDENCE"] == 1


def test_unverified_settlement_fair_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root)
        value = fair_status()
        value["contract"]["verified"] = False
        write(run_root / "external_fair/status.json", value)
        rows, status = bridge.build_maker_opportunities(
            run_root, now_ns=2_000_000_000, repository_root=ROOT,
        )
        assert rows == []
        assert status["state"] == "FAIL_CLOSED"
        assert "SETTLEMENT_FAIR_NOT_MATURE_OR_VERIFIED" in status["reasons"]


def test_semantic_hash_mismatch_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root)
        other = context()
        other["settlement_semantic_hash"] = "f" * 64
        with mock.patch.object(bridge, "require_context", return_value=other):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert rows == []
        assert status["state"] == "FAIL_CLOSED"
        assert status["reasons"] == ["SETTLEMENT_SEMANTIC_HASH_MISMATCH"]


if __name__ == "__main__":
    test_mature_positive_cell_becomes_typed_make_opportunity()
    test_immature_fill_model_does_not_manufacture_make_authority()
    test_unverified_settlement_fair_fails_closed()
    test_semantic_hash_mismatch_fails_closed()
