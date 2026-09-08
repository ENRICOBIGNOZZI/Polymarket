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
        "timestamp_ms": 1_000,
        "markets": [{
            "market_id": "market-1",
            "event_id": "event-1",
            "yes_token": "yes-token",
            "no_token": "no-token",
            "quote_opportunities": [quote],
            "control_exploration_authorized": True,
            "authorized_execution_cells": [{
                "outcome": "YES",
                "token_id": "yes-token",
                "quote_side": "BUY",
                "action": "JOIN",
                "authority_basis": "POSITIVE_FLOW_CONTROL",
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
        "policy_hash": "1e44b8d2ba4e8428",
        "config_hash": "88c9d5e1ebcc34ad",
        "execution_semantics_version": "maker-paper-v7.2-bilateral-inventory",
        "model_state": "MATURE" if mature else "EVIDENCE_ACCUMULATING",
        "artifact_role": "research",
        "research_runtime_model": True,
        "groups": {
            "JOIN:YES:BUY": {
                "mature": mature,
                "orders": 100 if mature else 10,
                "filled_orders": 50 if mature else 1,
                "event_clusters": 20 if mature else 1,
                "fill_probability": 0.50,
                "fill_probability_lower_90": 0.35 if mature else 0.0,
                "adverse_markout_per_share": 0.001,
            },
            "GLOBAL": {
                "mature": mature,
                "orders": 100 if mature else 10,
                "filled_orders": 50 if mature else 1,
                "event_clusters": 20 if mature else 1,
                "fill_probability": 0.50,
                "fill_probability_lower_90": 0.35 if mature else 0.0,
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
            "research_model": True,
            "research_model_state": "FROZEN_INFERENCE_ONLY",
            "probability_interval_validated": True,
            "real_money_authority": False,
            "authority": "SHADOW",
            "lower": 0.65,
            "yes": 0.70,
            "upper": 0.75,
            "tte_seconds": 90.0,
            "probability_model_hash": "e" * 64,
            "settlement_semantic_hash": SEMANTIC,
        },
    }


def bootstrap_fair_status() -> dict:
    value = fair_status()
    value["fair"].update({
        "research_model": False,
        "probability_interval_validated": False,
        "paper_exploration_bootstrap": True,
        "inference_state": "VALID_PAPER_EXPLORATION_BOOTSTRAP",
        "calibration_state": "PAPER_EXPLORATION_BOOTSTRAP_APPLIED",
        "probability_model_id": "btc_m5_same_oracle_diffusion_bootstrap_v1",
        "research_only": True,
        "real_money_authority": False,
        "uses_polymarket_price_as_feature": False,
        "authority": "SHADOW",
    })
    return value


def context() -> dict:
    return {
        "asset": "BTC",
        "horizon": "M5",
        "contract_family": "CRYPTO_UPDOWN",
        "settlement_semantic_hash": SEMANTIC,
        "authority": "PAPER_EXPLORATION",
        "research_only": False,
    }


def setup_run(root: Path, *, mature: bool = True) -> None:
    write(root / "control/runtime_status.json", runtime())
    write(root / "micro_maker/reward_selection.json", selection())
    write(root / "micro_maker/execution_model.json", model(mature=mature))
    write(root / "external_fair/status.json", fair_status())


def test_real_canonical_crypto_context_adapter_is_callable() -> None:
    registry = bridge.load_crypto_registry(ROOT / "config" / "v7_crypto_settlement_markets.json")
    context_value = bridge._paper_crypto_context(registry)
    assert context_value["asset"] == "BTC"
    assert context_value["horizon"] == "M5"
    assert context_value["authority"] == "PAPER_EXPLORATION"
    assert context_value["research_only"] is False
    assert len(context_value["settlement_semantic_hash"]) == 64
    assert context_value["contract_family"]


def test_mature_positive_cell_becomes_typed_make_opportunity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root)
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert status["state"] == "OPERATIONAL"
        assert len(rows) == 1
        row = OpportunityEnvelope.parse(rows[0]).raw
        assert row["action"] == "MAKE"
        assert row["engine_id"] == "CRYPTO_SETTLEMENT_ENGINE"
        assert row["component_provenance"] == ["professional_maker"]
        assert row["maker_execution_identity"] == {
            "policy_hash": "1e44b8d2ba4e8428",
            "config_hash": "88c9d5e1ebcc34ad",
            "execution_semantics_version": "maker-paper-v7.2-bilateral-inventory",
        }
        assert row["execution_plan"]["legs"][0]["side"] == "BUY"
        assert row["conservative_expected_wealth_change"] > 0.0
        assert row["execution_alpha"]["fill_probability"]["lower"] > 0.0
        assert row["execution_alpha"]["evidence_status"] == "MATURE"
        assert row["crypto_context"]["authority"] == "PAPER_EXPLORATION"
        assert row["crypto_context"]["research_only"] is False


def test_make_rejects_rich_pm_prior_that_is_stale_or_already_repriced() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root=Path(directory);setup_run(run_root,mature=False)
        fair=fair_status();fair['fair'].update({
            'uses_polymarket_price_as_feature':True,'market_prior_causal_cut_valid':True,
            'pm_mid':.51,'pm_mid_receive_ts_ms':1800})
        write(run_root/'external_fair/status.json',fair)
        with mock.patch.object(bridge,'_paper_crypto_context',return_value=context()):
            rows,status=bridge.build_maker_opportunities(run_root,now_ns=2_000_000_000,repository_root=ROOT)
        assert len(rows)==1,status
        fair['fair']['pm_mid']=.40;write(run_root/'external_fair/status.json',fair)
        with mock.patch.object(bridge,'_paper_crypto_context',return_value=context()):
            rows,status=bridge.build_maker_opportunities(run_root,now_ns=2_000_000_000,repository_root=ROOT)
        assert rows==[];assert status['rejected']['RICH_PM_PRIOR_STALE_OR_REPRICED']==1
        fair['fair']['pm_mid']=.51;fair['fair']['pm_mid_receive_ts_ms']=1000;write(run_root/'external_fair/status.json',fair)
        with mock.patch.object(bridge,'_paper_crypto_context',return_value=context()):
            rows,status=bridge.build_maker_opportunities(run_root,now_ns=2_000_000_000,repository_root=ROOT)
        assert rows==[];assert status['rejected']['RICH_PM_PRIOR_STALE_OR_REPRICED']==1


def test_current_run_research_model_updates_fill_posterior_directly() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root=Path(directory);setup_run(run_root,mature=False)
        research=model(mature=False)
        research["groups"]["JOIN:YES:BUY"].update(
            fill_probability=.0044,fill_probability_lower_90=.0008,orders=71,filled_orders=0,event_clusters=5,mature=False)
        research["groups"]["GLOBAL"].update(
            fill_probability=.0044,fill_probability_lower_90=.0008,orders=71,filled_orders=0,event_clusters=5,mature=False)
        write(run_root/"micro_maker/execution_model.json",research)
        with mock.patch.object(bridge,"_paper_crypto_context",return_value=context()):
            rows,status=bridge.build_maker_opportunities(run_root,now_ns=2_000_000_000,repository_root=ROOT)
        assert len(rows)==1,status
        row=OpportunityEnvelope.parse(rows[0]).raw
        assert status["research_execution_model"] is True
        assert status["research_evidence_scope"]=="CURRENT_RUN_ONLY"
        assert abs(row["execution_alpha"]["fill_probability"]["point"]-.0044)<1e-12
        assert row["execution_alpha"]["evidence_status"]=="IMMATURE"
        assert row["exploration"]["research_only"] is True

def test_immature_control_cell_becomes_bounded_research_paper_probe() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root, mature=False)
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert len(rows) == 1
        row = OpportunityEnvelope.parse(rows[0]).raw
        assert row["action"] == "MAKE"
        assert row["exploration"]["mode"] == "PAPER_BOOTSTRAP_PROBE"
        assert row["exploration"]["research_only"] is True
        assert row["exploration"]["robust_candidate"] is False
        assert row["exploration"]["model_id"] == "btc_m5_maker_execution_bootstrap_probe_v1"
        assert row["exploration"]["probe_loss_cap"] <= 2.0
        leg = row["execution_plan"]["legs"][0]
        assert leg["target_quantity"] * leg["limit_price"] <= 2.0 + 1e-9
        assert row["execution_alpha"]["evidence_status"] == "IMMATURE"
        assert status["typed_make_probe_opportunities"] == 1


def test_bootstrap_fair_can_only_power_loss_capped_paper_probe() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root, mature=True)
        write(run_root / "external_fair/status.json", bootstrap_fair_status())
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert len(rows) == 1
        row = OpportunityEnvelope.parse(rows[0]).raw
        assert row["exploration"]["mode"] == "PAPER_BOOTSTRAP_PROBE"
        assert row["exploration"]["research_only"] is True
        assert row["exploration"]["robust_candidate"] is False
        assert row["conservative_expected_wealth_change"] <= 0.0
        assert "STRUCTURAL_RESEARCH_FALLBACK" in row["reasons"]
        assert "FROZEN_RESEARCH_FAIR" not in row["reasons"]
        leg = row["execution_plan"]["legs"][0]
        assert leg["target_quantity"] * leg["limit_price"] <= 2.0 + 1e-9
        assert status["typed_make_probe_opportunities"] == 1


def test_settlement_anchor_cold_prior_cell_becomes_only_paper_probe() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root, mature=False)
        value = selection()
        value["markets"][0]["execution_role"] = "SETTLEMENT_ANCHOR_CONTROL"
        value["markets"][0]["settlement_anchor"] = True
        value["markets"][0]["authorized_execution_cells"][0].update({
            "authority_basis": "SETTLEMENT_ANCHOR_COLD_START_CONTROL",
            "projected_fill_probability": 0.02,
            "fill_probability_source": "EXECUTION_MODEL_COLD_PRIOR",
            "settlement_point_edge_per_share": 0.20,
        })
        value["markets"][0]["quote_opportunities"][0].update({
            "projected_join_fill_probability": 0.02,
            "projected_best_fill_probability": 0.02,
            "fill_probability_source": "EXECUTION_MODEL_COLD_PRIOR",
            "opposite_flow_shares_per_second": 0.0,
            "last_opposite_flow_age_ms": -1,
        })
        write(run_root / "micro_maker/reward_selection.json", value)
        write(run_root / "external_fair/status.json", bootstrap_fair_status())
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert len(rows) == 1, status
        row = OpportunityEnvelope.parse(rows[0]).raw
        assert row["exploration"]["mode"] == "PAPER_BOOTSTRAP_PROBE"
        assert row["exploration"]["research_only"] is True
        assert row["exploration"]["robust_candidate"] is False
        assert "STRUCTURAL_RESEARCH_FALLBACK" in row["reasons"]
        assert row["execution_alpha"]["fill_probability"]["point"] > 0.0
        assert row["execution_plan"]["legs"][0]["target_quantity"] \
            * row["execution_plan"]["legs"][0]["limit_price"] <= 2.0 + 1e-9
        assert status["typed_make_probe_opportunities"] == 1


def test_bootstrap_fair_cannot_power_noncontrol_or_robust_make() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root, mature=True)
        value = selection()
        value["markets"][0]["control_exploration_authorized"] = False
        write(run_root / "micro_maker/reward_selection.json", value)
        write(run_root / "external_fair/status.json", bootstrap_fair_status())
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert rows == []
        assert status["state"] == "NO_EXECUTABLE_MAKE"
        assert status["rejected"]["POINT_ONLY_FAIR_REQUIRES_PAPER_PROBE"] == 1


def test_bootstrap_fair_with_invalid_research_flag_or_pm_feature_fails_closed() -> None:
    for mutation in (
        lambda fair: fair.update(research_only=False),
        lambda fair: fair.update(uses_polymarket_price_as_feature=True),
        lambda fair: fair.update(real_money_authority=True),
    ):
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            setup_run(run_root, mature=False)
            value = bootstrap_fair_status()
            mutation(value["fair"])
            write(run_root / "external_fair/status.json", value)
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
            assert rows == []
            assert status["state"] == "FAIL_CLOSED"
            assert "SETTLEMENT_RESEARCH_FAIR_NOT_READY" in status["reasons"]


def test_immature_noncontrol_cell_still_cannot_manufacture_make_authority() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root, mature=False)
        value = selection()
        value["markets"][0]["control_exploration_authorized"] = False
        write(run_root / "micro_maker/reward_selection.json", value)
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
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
        assert "SETTLEMENT_RESEARCH_FAIR_NOT_READY" in status["reasons"]


def test_semantic_hash_mismatch_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root)
        other = context()
        other["settlement_semantic_hash"] = "f" * 64
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=other):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert rows == []
        assert status["state"] == "FAIL_CLOSED"
        assert status["reasons"] == ["SETTLEMENT_SEMANTIC_HASH_MISMATCH"]


def test_sell_cell_is_not_advertised_before_canonical_inventory_bridge() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root)
        value = selection()
        quote = dict(value["markets"][0]["quote_opportunities"][0])
        quote["quote_side"] = "SELL"
        value["markets"][0]["quote_opportunities"] = [quote]
        value["markets"][0]["authorized_execution_cells"] = [{
            "outcome": "YES", "token_id": "yes-token", "quote_side": "SELL",
            "action": "JOIN", "maximum_quote_shares": 5.0,
        }]
        write(run_root / "micro_maker/reward_selection.json", value)
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert rows == []
        assert status["rejected"]["SELL_REQUIRES_CANONICAL_INVENTORY_BRIDGE"] == 1
        assert status["supported_execution_sides"] == ["BUY"]


def test_canonical_selector_timestamp_is_receive_time_causal_and_stale_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        run_root = Path(directory)
        setup_run(run_root)
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, _ = bridge.build_maker_opportunities(
                run_root, now_ns=2_000_000_000, repository_root=ROOT,
            )
        assert len(rows) == 1
        assert rows[0]["source_event_timestamps_ns"] == [1_000_000_000]
        assert rows[0]["execution_alpha"]["feature_receive_timestamp_ns"] == 1_000_000_000

        value = selection()
        value["timestamp_ms"] = 1
        write(run_root / "micro_maker/reward_selection.json", value)
        with mock.patch.object(bridge, "_paper_crypto_context", return_value=context()):
            rows, status = bridge.build_maker_opportunities(
                run_root, now_ns=30_000_000_000, repository_root=ROOT,
            )
        assert rows == []
        assert "MAKER_SELECTION_STALE_OR_NONCAUSAL" in status["reasons"]


if __name__ == "__main__":
    test_mature_positive_cell_becomes_typed_make_opportunity()
    test_immature_control_cell_becomes_bounded_research_paper_probe()
    test_bootstrap_fair_can_only_power_loss_capped_paper_probe()
    test_settlement_anchor_cold_prior_cell_becomes_only_paper_probe()
    test_bootstrap_fair_cannot_power_noncontrol_or_robust_make()
    test_bootstrap_fair_with_invalid_research_flag_or_pm_feature_fails_closed()
    test_immature_noncontrol_cell_still_cannot_manufacture_make_authority()
    test_unverified_settlement_fair_fails_closed()
    test_semantic_hash_mismatch_fails_closed()
    test_sell_cell_is_not_advertised_before_canonical_inventory_bridge()
    test_canonical_selector_timestamp_is_receive_time_causal_and_stale_fails_closed()
