from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]

import v7_external_cancel_opportunity_bridge as cancel_bridge  # noqa: E402
import v7_global_portfolio_coordinator as coordinator  # noqa: E402
import v7_maker_opportunity_bridge as maker_bridge  # noqa: E402
import test_v7_maker_opportunity_bridge as fixture  # noqa: E402

SHA = "a" * 40
RULE_SHA = "b" * 64


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def read_status(root: Path) -> dict:
    try:
        return json.loads(
            (root / "micro_maker/authorized_make_executor_status.json").read_text()
        )
    except (OSError, json.JSONDecodeError):
        return {}


def wait_for(root: Path, predicate, *, attempts: int = 150) -> dict:
    for _ in range(attempts):
        status = read_status(root)
        if predicate(status):
            return status
        time.sleep(0.02)
    raise AssertionError(f"executor status condition not reached: {read_status(root)}")


def run(executor: Path) -> None:
    assert executor.is_file(), executor
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        now_ms = time.time_ns() // 1_000_000
        runtime = fixture.runtime(); runtime["model_sha"] = SHA
        selection = fixture.selection(); selection["timestamp_ms"] = now_ms
        model = fixture.model(mature=True); model["model_sha"] = SHA
        fair = fixture.fair_status(); fair["code_sha"] = SHA
        write(root / "control/runtime_status.json", runtime)
        write(root / "micro_maker/reward_selection.json", selection)
        write(root / "micro_maker/execution_model.json", model)
        write(root / "external_fair/status.json", fair)
        write(root / "micro_maker/fillability_ws_status.json", {
            "schema": "polymarket_v7_maker_fillability_ws_status_v1",
            "model_sha": SHA, "state": "running", "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "evidence_complete": True, "timestamp_ms": now_ms,
            "last_exchange_event_ns": time.monotonic_ns(),
        })
        (root / "micro_maker/fillability_ws.jsonl").touch()

        original_require_context = maker_bridge.require_context
        maker_bridge.require_context = lambda **_: fixture.context() | {
            "authority": "SHADOW", "research_only": False,
        }
        try:
            now_ns = time.time_ns()
            maker_rows, maker_diag = maker_bridge.build_maker_opportunities(
                root, now_ns=now_ns, repository_root=ROOT,
            )
        finally:
            maker_bridge.require_context = original_require_context
        assert maker_rows, maker_diag
        make_decision = coordinator.coordinate(
            maker_rows, now_ns=now_ns,
            new_risk_authorized=False, paper_exploration_authorized=True,
        )
        assert make_decision["action"] == "MAKE"
        coordinator._publish_make_authorization(root, make_decision, maker_rows)

        process = subprocess.Popen(
            [str(executor), "--run-root", str(root), "--model-sha", SHA],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            wait_for(root, lambda row: int(row.get("submitted_orders") or 0) == 1)
            trigger_ns = time.time_ns()
            write(root / "control/external_cancel_activation.json", {
                "schema": cancel_bridge.ACTIVATION_SCHEMA,
                "experiment_id": cancel_bridge.EXPERIMENT_ID,
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "real_money_authority": False,
                "automatic_promotion": False,
                "paper_execution_alpha_overlay_eligible": True,
                "manual_exact_sha_promotion_required": True,
                "frozen_rule_retuning_allowed": False, "failed_checks": [],
                "evidence": {"rule_sha256": RULE_SHA},
            })
            write(root / "external_fair/external_cancel_signal.json", {
                "schema": cancel_bridge.SIGNAL_SCHEMA,
                "experiment_id": cancel_bridge.EXPERIMENT_ID,
                "code_sha": SHA, "rule_sha256": RULE_SHA,
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "real_money_authority": False,
                "automatic_promotion": False,
                "execution_authority": "ZERO_AUTHORITY_SIGNAL_ONLY",
                "shock_source": "BINANCE_SPOT_TRADES",
                "confirmation_source": "COINBASE_SPOT_TOP_OF_BOOK",
                "confirmation": "NON_OPPOSING", "shock_window_ms": 100,
                "minimum_absolute_log_return_bp": 0.30,
                "trigger_cooldown_ms": 250, "trigger_grid_ms": 25,
                "overlap_warmup_ms": 300, "maximum_live_signal_age_ms": 100,
                "supported_cancel_side": "BUY", "confirmed_non_opposing": True,
                "valid": True, "signal_version": 1, "stale_buy_outcome": "YES",
                "trigger_receive_wall_ns": trigger_ns - 5_000_000,
                "publish_wall_ns": trigger_ns - 2_000_000,
                "valid_until_wall_ns": trigger_ns + 80_000_000,
            })
            cancel_rows, cancel_diag = cancel_bridge.build_external_cancel_opportunities(
                root, now_ns=trigger_ns,
            )
            assert cancel_rows, cancel_diag
            cancel_decision = coordinator.coordinate(
                cancel_rows, now_ns=trigger_ns,
                new_risk_authorized=False, paper_exploration_authorized=True,
            )
            assert cancel_decision["action"] == "CANCEL"
            coordinator._publish_cancel_authorization(
                root, cancel_decision, cancel_rows,
            )
            final = wait_for(
                root,
                lambda row: int(row.get("coordinator_cancel_requests") or 0) == 1
                and int(row.get("terminal_orders") or 0) == 1,
            )
            assert final["active_orders"] == 0
            assert final["last_terminal_reason"] == "CANCELLED"
            assert final["rejected_authorizations"] == 0
            assert final["rejected_cancel_authorizations"] == 0
            assert final["fills"] == 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=2)
        assert process.returncode in {-15, -2, 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executor", type=Path, required=True)
    args = parser.parse_args()
    run(args.executor)


if __name__ == "__main__":
    main()
