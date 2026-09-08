from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_external_cancel_opportunity_bridge as bridge  # noqa: E402
from v7_opportunity import OpportunityEnvelope  # noqa: E402

SHA = "a" * 40
RULE = bridge.FROZEN_RULE_SHA
SEMANTIC = "c" * 64
NOW = 10_000_000_000


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def runtime() -> dict:
    return {
        "schema": "polymarket_v7_runtime_status_v3", "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "model_sha": SHA, "config_hash": "d" * 40,
        "policy_hash": "e" * 40, "run_id": "run-1",
    }


def activation(active: bool = True) -> dict:
    return {
        "schema": bridge.ACTIVATION_SCHEMA, "experiment_id": bridge.EXPERIMENT_ID,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_money_authority": False,
        "automatic_promotion": False,
        "paper_execution_alpha_overlay_eligible": active,
        "manual_exact_sha_promotion_required": True,
        "frozen_rule_retuning_allowed": False,
        "failed_checks": [] if active else ["state_pass"],
        "evidence": {
            "rule_sha256": bridge.FROZEN_RULE_SHA,
            "official_v3_provenance_verified": True,
            "official_v3_promotion_boundary_ms": bridge.OFFICIAL_V3_PROMOTION_BOUNDARY_MS,
            "official_v3_protocol_reference_sha256": bridge.OFFICIAL_V3_PROTOCOL_SHA,
            "activation_report_sha256": "f" * 64,
        },
    }


def signal(*, stale: str = "YES", valid_until: int = NOW + 50_000_000) -> dict:
    return {
        "schema": bridge.SIGNAL_SCHEMA, "experiment_id": bridge.EXPERIMENT_ID,
        "code_sha": SHA, "rule_sha256": RULE,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_money_authority": False,
        "automatic_promotion": False, "execution_authority": "ZERO_AUTHORITY_SIGNAL_ONLY",
        "shock_source": "BINANCE_SPOT_TRADES", "confirmation_source": "COINBASE_SPOT_TOP_OF_BOOK",
        "confirmation": "NON_OPPOSING", "shock_window_ms": 100,
        "minimum_absolute_log_return_bp": 0.30, "trigger_cooldown_ms": 250,
        "trigger_grid_ms": 25, "overlap_warmup_ms": 300,
        "maximum_live_signal_age_ms": 100, "supported_cancel_side": "BUY",
        "confirmed_non_opposing": True, "valid": True, "signal_version": 7,
        "stale_buy_outcome": stale, "trigger_receive_wall_ns": NOW - 10_000_000,
        "publish_wall_ns": NOW - 5_000_000, "valid_until_wall_ns": valid_until,
    }


def make_envelope(outcome: str = "YES") -> dict:
    raw = {
        "schema": "polymarket_v7_opportunity_envelope_v1", "version": 1,
        "model_sha": SHA, "config_hash": "d" * 40, "policy_hash": "e" * 40,
        "run_id": "run-1", "source_snapshot_identity": "selection-1",
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE", "component_provenance": ["professional_maker"],
        "market_id": "market-1", "event_id": "event-1", "contract_id": f"{outcome.lower()}-token",
        "mapping_identity": SEMANTIC,
        "crypto_context": {"asset": "BTC", "horizon": "M5", "contract_family": "CRYPTO_UPDOWN",
                           "settlement_semantic_hash": SEMANTIC, "authority": "PAPER_EXPLORATION",
                           "research_only": False},
        "action": "MAKE", "side": outcome,
        "decision_receive_timestamp_ns": NOW - 2_000_000_000,
        "source_event_timestamps_ns": [NOW - 2_100_000_000],
        "fair_value": {"lower": 0.60, "point": 0.65, "upper": 0.70},
        "conservative_expected_wealth_change": 0.01,
        "cost_vector": {"fee": 0.0, "slippage": 0.0, "unwind_loss": 0.001, "capital_cost": 0.0,
                        "latency_cost": 0.001, "adverse_markout": 0.001, "rebate": 0.0},
        "cost_authority": {"fee": "AUTHORITATIVE", "slippage": "CONSERVATIVE_ZERO",
                           "unwind_loss": "CONSERVATIVE_BOUND", "capital_cost": "CONSERVATIVE_ZERO",
                           "latency_cost": "CONSERVATIVE_BOUND", "adverse_markout": "CONSERVATIVE_BOUND",
                           "rebate": "CONSERVATIVE_ZERO"},
        "uncertainty": {"lower_bound": 0.01, "upper_bound": 0.02, "status": "MATURE"},
        "calibration_status": "MATURE",
        "latency": {"profile_id": "maker", "profile_valid": True, "economic_percentile": "p99", "arrival_ns": 1},
        "capacity": {"executable_size": 5.0, "depth_provenance": "selection-1"},
        "execution_plan": {"atomic_unit_id": "maker-unit", "execution_style": "SINGLE_LEG",
                           "legs": [{"leg_id": "maker-leg", "market_id": "market-1",
                                     "contract_id": f"{outcome.lower()}-token", "token_id": f"{outcome.lower()}-token",
                                     "side": "BUY", "target_quantity": 5.0, "limit_price": 0.50,
                                     "fee_authority": "AUTHORITATIVE"}],
                           "partial_fill_plan": "CANCEL_REMAINDER", "timeout_ms": 5000, "unwind_plan": "NONE"},
        "inventory_delta": 5.0, "portfolio_exposure_delta": 2.5,
        "settlement": {"definition": "rule-bound", "source": "POLYMARKET_RULE_BOUND_ORACLE", "verified": True},
        "eligible": True, "reasons": ["TEST"], "deterministic_replay_key": f"maker-{outcome.lower()}",
        "expires_at_ns": NOW + 5_000_000_000,
    }
    return OpportunityEnvelope.parse(raw).raw


def setup(root: Path, *, outcome: str = "YES", gate: bool = True, live_signal: dict | None = None) -> None:
    write(root / "control/runtime_status.json", runtime())
    write(root / "control/external_cancel_activation.json", activation(gate))
    write(root / "external_fair/external_cancel_signal.json", live_signal or signal(stale=outcome))
    envelope = make_envelope(outcome)
    write(root / "micro_maker/authorized_make/live/order.json", {
        "schema": "polymarket_v7_authorized_make_intent_v1", "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False, "real_capital_at_risk": False,
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR", "execution_authority": "SIMULATED_PAPER_ONLY",
        "decision": {"action": "MAKE", "engine_id": "CRYPTO_SETTLEMENT_ENGINE"},
        "opportunity_envelope": envelope,
    })
    write(root / "micro_maker/authorized_make_executor_status.json", {
        "schema": bridge.EXECUTOR_STATUS_SCHEMA, "timestamp_ms": NOW // 1_000_000,
        "model_sha": SHA, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "execution_authority": "SIMULATED_PAPER_ONLY",
        "active_order_details": [{
            "order_id": "17", "replay_key": envelope["deterministic_replay_key"],
            "market_id": envelope["market_id"], "event_id": envelope["event_id"],
            "token_id": envelope["execution_plan"]["legs"][0]["token_id"],
            "outcome": outcome, "side": "BUY", "limit_price": 0.50,
            "remaining_shares": 3.5, "cancel_requested": False,
        }],
    })


def test_active_frozen_signal_builds_typed_cancel() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); setup(root)
        rows, status = bridge.build_external_cancel_opportunities(root, now_ns=NOW)
        assert status["state"] == "ACTIVE" and len(rows) == 1
        row = OpportunityEnvelope.parse(rows[0]).raw
        assert row["action"] == "CANCEL" and row["side"] == "NONE"
        assert row["execution_plan"]["legs"][0]["side"] == "BUY"
        assert row["execution_plan"]["legs"][0]["leg_id"] == "17"
        assert row["execution_plan"]["legs"][0]["target_quantity"] == 3.5
        assert row["execution_plan"]["atomic_unit_id"] == "maker-yes"
        assert row["execution_plan"]["unwind_plan"] == "CANCEL_ONLY"
        assert row["source_event_timestamps_ns"] == [NOW - 10_000_000]
        assert status["exact_order_targeting"] is True


def test_inactive_forward_gate_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); setup(root, gate=False)
        rows, status = bridge.build_external_cancel_opportunities(root, now_ns=NOW)
        assert rows == [] and "EXTERNAL_CANCEL_FORWARD_GATE_NOT_ACTIVE" in status["reasons"]


def test_signal_only_targets_stale_buy_outcome() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); setup(root, outcome="YES", live_signal=signal(stale="NO"))
        rows, status = bridge.build_external_cancel_opportunities(root, now_ns=NOW)
        assert rows == [] and status["state"] == "NO_MATCHING_ACTIVE_BUY_QUOTES"


def test_same_token_replacement_order_does_not_match_stale_authorization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); setup(root)
        status_path = root / "micro_maker/authorized_make_executor_status.json"
        status = json.loads(status_path.read_text())
        status["active_order_details"][0]["replay_key"] = "replacement-make"
        status["active_order_details"][0]["order_id"] = "999"
        write(status_path, status)
        rows, diagnostics = bridge.build_external_cancel_opportunities(root, now_ns=NOW)
        assert rows == []
        assert diagnostics["state"] == "NO_MATCHING_ACTIVE_BUY_QUOTES"


def test_missing_executor_order_state_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); setup(root)
        (root / "micro_maker/authorized_make_executor_status.json").unlink()
        rows, diagnostics = bridge.build_external_cancel_opportunities(root, now_ns=NOW)
        assert rows == []
        assert "MAKER_EXECUTOR_ACTIVE_ORDER_STATE_NOT_READY" in diagnostics["reasons"]


def test_expired_signal_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); setup(root, live_signal=signal(valid_until=NOW))
        rows, status = bridge.build_external_cancel_opportunities(root, now_ns=NOW)
        assert rows == [] and "EXTERNAL_CANCEL_LIVE_SIGNAL_NOT_ACTIVE" in status["reasons"]


if __name__ == "__main__":
    test_active_frozen_signal_builds_typed_cancel()
    test_inactive_forward_gate_fails_closed()
    test_signal_only_targets_stale_buy_outcome()
    test_expired_signal_fails_closed()
