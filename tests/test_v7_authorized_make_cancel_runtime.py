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
RULE_SHA = cancel_bridge.FROZEN_RULE_SHA


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


def spool_rows(root: Path) -> list[dict]:
    rows = []
    for path in sorted((root / "ledger/spool").glob("*.json")):
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


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

        original_paper_context = maker_bridge._paper_crypto_context
        maker_bridge._paper_crypto_context = lambda _registry: fixture.context()
        try:
            now_ns = time.time_ns()
            maker_rows, maker_diag = maker_bridge.build_maker_opportunities(
                root, now_ns=now_ns, repository_root=ROOT,
            )
        finally:
            maker_bridge._paper_crypto_context = original_paper_context
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
            submitted = wait_for(
                root,
                lambda row: int(row.get("submitted_orders") or 0) == 1
                and len(row.get("active_order_details") or []) == 1,
            )
            active_before_cancel = submitted["active_order_details"][0]
            assert active_before_cancel["order_id"]
            assert active_before_cancel["replay_key"] == maker_rows[0]["deterministic_replay_key"]
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
                "evidence": {
                    "rule_sha256": RULE_SHA,
                    "official_v3_provenance_verified": True,
                    "official_v3_promotion_boundary_ms": cancel_bridge.OFFICIAL_V3_PROMOTION_BOUNDARY_MS,
                    "official_v3_protocol_reference_sha256": cancel_bridge.OFFICIAL_V3_PROTOCOL_SHA,
                    "activation_report_sha256": "f" * 64,
                },
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
            target = cancel_rows[0]["execution_plan"]
            assert target["atomic_unit_id"] == active_before_cancel["replay_key"]
            assert target["legs"][0]["leg_id"] == active_before_cancel["order_id"]
            coordinator._publish_cancel_authorization(
                root, cancel_decision, cancel_rows,
            )
            requested = wait_for(
                root,
                lambda row: int(row.get("coordinator_cancel_requests") or 0) == 1
                and len(row.get("active_order_details") or []) == 1
                and row["active_order_details"][0].get("cancel_requested") is True,
            )
            active = requested["active_order_details"][0]
            cancel_ns = int(active["cancel_requested_monotonic_ns"])
            arrival_exchange_ns = int(active["arrival_exchange_event_ns"])
            # The canonical PaperMakerMarketEngine has 100 ms cancel latency.
            # One 8-share SELL print consumes the 7.5-share pessimistic queue
            # ahead and fills only 0.5 share before cancellation becomes
            # effective. A much larger print after +150 ms must not fill the
            # remaining 4.5 shares.
            trade_rows = [
                {
                    "schema": "polymarket_v7_maker_fillability_ws_trade_v1",
                    "model_sha": SHA, "paper_only": True,
                    "authenticated_execution": False, "real_order_submission": False,
                    "observer_sequence": 101, "market_id": "market-1",
                    "event_id": "event-1", "token_id": "yes-token",
                    "instrument_handle": 1, "state_version": 101, "connection_epoch": 1,
                    "exchange_event_ns": arrival_exchange_ns + 10_000_000,
                    "receive_wall_ms": time.time_ns() // 1_000_000,
                    "receive_monotonic_ns": cancel_ns + 50_000_000,
                    "aggressor_side": "SELL", "price": 0.50, "size": 8.0,
                    "lineage_continuous": True,
                },
                {
                    "schema": "polymarket_v7_maker_fillability_ws_trade_v1",
                    "model_sha": SHA, "paper_only": True,
                    "authenticated_execution": False, "real_order_submission": False,
                    "observer_sequence": 102, "market_id": "market-1",
                    "event_id": "event-1", "token_id": "yes-token",
                    "instrument_handle": 1, "state_version": 102, "connection_epoch": 1,
                    "exchange_event_ns": arrival_exchange_ns + 20_000_000,
                    "receive_wall_ms": time.time_ns() // 1_000_000,
                    "receive_monotonic_ns": cancel_ns + 150_000_000,
                    "aggressor_side": "SELL", "price": 0.50, "size": 100.0,
                    "lineage_continuous": True,
                },
            ]
            with (root / "micro_maker/fillability_ws.jsonl").open("a", encoding="utf-8") as handle:
                for trade in trade_rows:
                    handle.write(json.dumps(trade) + "\n")
                handle.flush()
            final = wait_for(
                root,
                lambda row: int(row.get("trade_rows_consumed") or 0) >= 2
                and int(row.get("terminal_orders") or 0) == 1,
            )
            assert final["active_orders"] == 0
            assert final["last_terminal_reason"] == "CANCELLED"
            assert final["rejected_authorizations"] == 0
            assert final["rejected_cancel_authorizations"] == 0
            assert final["fills"] >= 1
            fills = [row for row in spool_rows(root) if row.get("event_type") == "FILL"]
            assert len(fills) == 1
            assert abs(float(fills[0]["filled_size"]) - 0.5) < 1e-12
            assert fills[0]["position_id"]
            assert fills[0]["metadata"]["excluded_from_portfolio_equity"] is False
            # A different market's engine also starts at native ID 1. Its public
            # order identity and executor-map entry must nevertheless be unique.
            def second_market(value):
                rendered = json.dumps(value)
                for old, new in (("market-1", "market-2"), ("event-1", "event-2"),
                                 ("yes-token", "yes-token-2"), ("no-token", "no-token-2")):
                    rendered = rendered.replace(old, new)
                return json.loads(rendered)
            selection2 = second_market(fixture.selection())
            selection2["timestamp_ms"] = time.time_ns() // 1_000_000
            write(root / "micro_maker/reward_selection.json", selection2)
            write(root / "external_fair/status.json", second_market(fixture.fair_status()))
            maker_bridge._paper_crypto_context = lambda _registry: fixture.context()
            try:
                now_ns = time.time_ns()
                maker2, diag2 = maker_bridge.build_maker_opportunities(root, now_ns=now_ns, repository_root=ROOT)
            finally:
                maker_bridge._paper_crypto_context = original_paper_context
            assert maker2, diag2
            decision2 = coordinator.coordinate(maker2, now_ns=now_ns,
                new_risk_authorized=False, paper_exploration_authorized=True)
            coordinator._publish_make_authorization(root, decision2, maker2)
            next_order = wait_for(root, lambda r: r.get("submitted_orders") == 2 and r.get("active_orders") == 1)
            new_id = next_order["active_order_details"][0]["order_id"]
            assert new_id != active_before_cancel["order_id"]
            assert next_order["active_order_details"][0]["market_id"] == "market-2"
            # Canonical drain is a real cancel, not a fake terminal state. It
            # must use the same 100ms cancel lifecycle and reject new MAKEs.
            write(root / "control/CUTOVER_DRAIN", {"paper_only":True})
            drained = wait_for(root, lambda r: r.get("terminal_orders") == 2 and r.get("active_orders") == 0)
            assert drained["last_terminal_reason"] == "CANCELLED"
            coordinator._publish_make_authorization(root, decision2, maker2)
            refused = wait_for(root, lambda r: r.get("rejected_authorizations",0) >= 1)
            assert refused["submitted_orders"] == 2
            assert refused["last_error"] == "CANONICAL_DRAIN_OR_KILL_NO_NEW_MAKE"
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
