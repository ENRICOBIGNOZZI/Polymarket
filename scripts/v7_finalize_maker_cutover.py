#!/usr/bin/env python3
"""Flatten verified PAPER maker inventory after runtime stop, before SHA archive."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import drain_spool, spool_events


STRATEGY = "MICRO_MAKER_PRO"


class MakerCutoverError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MakerCutoverError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise MakerCutoverError(f"invalid_object:{path.name}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def number(name: str, value: Any, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MakerCutoverError(f"invalid_number:{name}") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise MakerCutoverError(f"invalid_number:{name}")
    return result


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:32]


def object_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def maker_flat(state: dict[str, Any]) -> bool:
    inventory = state.get("inventory")
    if not isinstance(inventory, dict):
        return False
    for row in inventory.values():
        if not isinstance(row, dict):
            return False
        for outcome in ("yes", "no"):
            try:
                shares = float(row.get(f"{outcome}_shares") or 0.0)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(shares) or shares < 0.0 or shares > 1e-9:
                return False
    return True


def reconcile_invalid_spool(root: Path, model_sha: str, nonce: str) -> dict[str, Any]:
    """Losslessly quarantine records that can never enter the canonical ledger."""
    spool = root / "ledger/spool"
    quarantine = root / "ledger/rejected_cutover" / stable_id(nonce)
    quarantine.mkdir(parents=True, exist_ok=True)
    for path in sorted(spool.glob("*.json")) if spool.exists() else []:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            event = LedgerEvent.from_dict(raw)
            if event.model_sha != model_sha:
                raise MakerCutoverError("mixed_model_sha")
        except Exception:
            destination = quarantine / path.name
            if destination.exists():
                if destination.read_bytes() != path.read_bytes():
                    raise MakerCutoverError("spool_quarantine_name_collision")
                path.unlink()
            else:
                os.replace(path, destination)

    rejected: list[dict[str, Any]] = []
    for path in sorted(quarantine.glob("*.json")):
        payload = path.read_bytes()
        reason = "unknown"
        try:
            raw = json.loads(payload)
            event = LedgerEvent.from_dict(raw)
            if event.model_sha != model_sha:
                reason = "mixed_model_sha"
            else:
                raise MakerCutoverError("valid_record_in_rejected_quarantine")
        except MakerCutoverError as exc:
            if str(exc) == "valid_record_in_rejected_quarantine":
                raise
            reason = str(exc)
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"
        rejected.append({
            "file": path.name, "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(), "reason": reason,
        })
    receipt = {
        "schema": "polymarket_v7_cutover_spool_reconciliation_v1",
        "timestamp_ms": time.time_ns() // 1_000_000,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "model_sha": model_sha, "nonce": nonce,
        "rejected_records": rejected, "rejected_count": len(rejected),
        "quarantine": str(quarantine.relative_to(root)),
    }
    atomic_json(root / "control" / f"spool_reconciliation.{stable_id(nonce)}.json", receipt)
    return receipt


def resume_transaction(
    root: Path,
    model_sha: str,
    nonce: str,
    journal: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Resume the durable PAPER liquidation transaction exactly once."""
    state_path = root / "micro_maker/state.json"
    status_path = root / "micro_maker/status.json"
    receipt_path = root / "control/maker_cutover_liquidation.json"
    if (
        journal.get("schema") != "polymarket_v7_maker_cutover_liquidation_v1"
        or journal.get("nonce") != nonce
        or journal.get("model_sha") != model_sha
        or journal.get("paper_only") is not True
        or journal.get("authenticated_execution") is not False
        or journal.get("real_order_submission") is not False
    ):
        raise MakerCutoverError("maker_liquidation_journal_mismatch")

    final_digest = str(journal.get("final_state_digest") or "")
    current_digest = object_digest(state)
    if journal.get("state") == "MAKER_FLAT":
        live_status = read_json(status_path)
        if (
            current_digest != final_digest
            or state.get("cutover_liquidation_nonce") != nonce
            or not maker_flat(state)
            or live_status.get("model_sha") != model_sha
            or live_status.get("paper_only") is not True
            or live_status.get("authenticated_execution") is not False
            or live_status.get("drain_complete") is not True
            or live_status.get("positions") != []
            or abs(number("completed_cash", live_status.get("cash")) - number("state_cash", state.get("cash"))) > 1e-8
            or abs(
                number("completed_realized_pnl", live_status.get("realized_trading_pnl") or 0.0)
                - number("state_realized_pnl", state.get("realized_trading_pnl") or 0.0)
            ) > 1e-8
        ):
            raise MakerCutoverError("completed_maker_liquidation_state_mismatch")
        return journal
    if journal.get("state") != "LIQUIDATION_PENDING":
        raise MakerCutoverError("maker_liquidation_journal_state_invalid")

    updated_state = journal.get("updated_state")
    final_status = journal.get("final_status")
    raw_events = journal.get("events")
    if not isinstance(updated_state, dict) or not isinstance(final_status, dict) or not isinstance(raw_events, list):
        raise MakerCutoverError("maker_liquidation_journal_shape_invalid")
    if object_digest(updated_state) != final_digest:
        raise MakerCutoverError("maker_liquidation_final_digest_mismatch")
    if (
        updated_state.get("model_sha") != model_sha
        or updated_state.get("cutover_liquidation_nonce") != nonce
        or not maker_flat(updated_state)
        or final_status.get("model_sha") != model_sha
        or final_status.get("paper_only") is not True
        or final_status.get("authenticated_execution") is not False
        or final_status.get("positions") != []
        or final_status.get("drain_complete") is not True
    ):
        raise MakerCutoverError("maker_liquidation_terminal_state_invalid")

    if current_digest == str(journal.get("original_state_digest") or ""):
        try:
            events = [LedgerEvent.from_dict(raw) for raw in raw_events]
        except Exception as exc:
            raise MakerCutoverError("maker_liquidation_journal_events_invalid") from exc
        reconcile_invalid_spool(root, model_sha, nonce)
        spool_events(root, events)
        drained = drain_spool(root, model_sha=model_sha, writer_id=f"cutover-maker-{nonce}")
        if drained["rejected"]:
            raise MakerCutoverError("maker_liquidation_spool_rejected")
        atomic_json(state_path, updated_state)
    elif current_digest != final_digest:
        raise MakerCutoverError("maker_liquidation_state_diverged")

    # Rewriting these atomic files is intentional: it closes crashes after the
    # ledger append, state commit, or status commit without double-accounting.
    atomic_json(status_path, final_status)
    completed = {
        key: value for key, value in journal.items()
        if key not in {"events", "updated_state", "final_status", "original_state_digest"}
    }
    completed["state"] = "MAKER_FLAT"
    atomic_json(receipt_path, completed)
    return completed


def finalize(
    run_root: Path,
    model_sha: str,
    nonce: str,
    *,
    mark_path: Path | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    root = Path(run_root)
    control = root / "control"
    receipt_path = control / "maker_cutover_liquidation.json"
    prior_receipt = read_json(receipt_path) if receipt_path.exists() else {}

    sentinel = read_json(control / "CUTOVER_DRAIN")
    state_path = root / "micro_maker/state.json"
    status_path = root / "micro_maker/status.json"
    state = read_json(state_path)
    status = read_json(mark_path if mark_path is not None else status_path)
    if sentinel.get("schema") != "polymarket_v7_cutover_drain_v1" or sentinel.get("nonce") != nonce:
        raise MakerCutoverError("cutover_drain_identity_mismatch")
    if sentinel.get("current_sha") != model_sha or sentinel.get("paper_only") is not True:
        raise MakerCutoverError("cutover_drain_safety_mismatch")
    for name, value in (("state", state),):
        if value.get("paper_only") is not True or value.get("authenticated_execution") is not False:
            raise MakerCutoverError(f"unsafe_maker_{name}")
    if state.get("model_sha") != model_sha:
        raise MakerCutoverError("maker_model_sha_mismatch")
    if prior_receipt.get("nonce") == nonce:
        return resume_transaction(root, model_sha, nonce, prior_receipt, state)

    if status.get("paper_only") is not True or status.get("authenticated_execution") is not False:
        raise MakerCutoverError("unsafe_maker_status")
    if status.get("model_sha") != model_sha:
        raise MakerCutoverError("maker_model_sha_mismatch")
    if status.get("marking_complete") is not True or status.get("killed") is True:
        raise MakerCutoverError("maker_not_safely_marked")
    if "drain_requested" in status and status.get("drain_requested") is not True:
        raise MakerCutoverError("maker_drain_not_observed")

    current_ms = int(time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    status_ms = int(number("status_timestamp_ms", status.get("timestamp_ms"), minimum=1.0))
    if current_ms < status_ms or current_ms - status_ms > 15_000:
        raise MakerCutoverError("maker_mark_stale")
    inventory = state.get("inventory")
    marks = status.get("positions")
    if not isinstance(inventory, dict) or not isinstance(marks, list):
        raise MakerCutoverError("maker_inventory_shape_invalid")

    by_identity: dict[tuple[str, str], tuple[dict[str, Any], str, str]] = {}
    for market_id, row in inventory.items():
        if not isinstance(row, dict):
            raise MakerCutoverError("maker_inventory_row_invalid")
        for outcome in ("yes", "no"):
            token = str(row.get(f"{outcome}_token") or "")
            shares = number(f"{market_id}.{outcome}_shares", row.get(f"{outcome}_shares") or 0.0, minimum=0.0)
            if shares > 1e-9:
                if not token:
                    raise MakerCutoverError("maker_inventory_token_missing")
                by_identity[(str(market_id), token)] = (row, outcome, str(row.get("condition_id") or ""))
    if len(by_identity) != len(marks):
        raise MakerCutoverError("maker_mark_inventory_count_mismatch")

    events: list[LedgerEvent] = []
    total_net = 0.0
    total_pnl = 0.0
    liquidations: list[dict[str, Any]] = []
    updated = copy.deepcopy(state)
    updated_inventory = updated["inventory"]
    for mark in marks:
        if not isinstance(mark, dict):
            raise MakerCutoverError("maker_mark_row_invalid")
        market_id = str(mark.get("market_id") or "")
        token_id = str(mark.get("token_id") or "")
        identity = (market_id, token_id)
        if identity not in by_identity:
            raise MakerCutoverError("maker_mark_inventory_identity_mismatch")
        row, outcome, condition_id = by_identity.pop(identity)
        shares = number("shares", mark.get("shares"), minimum=0.0)
        durable_shares = number("durable_shares", row.get(f"{outcome}_shares"), minimum=0.0)
        if shares <= 1e-9 or abs(shares - durable_shares) > 1e-8:
            raise MakerCutoverError("maker_mark_share_mismatch")
        cost = number("cost", row.get(f"{outcome}_cost"), minimum=0.0)
        method = str(mark.get("liquidation_method") or "DIRECT_SELL")
        execution_token = str(mark.get("execution_token_id") or token_id)
        execution_side = str(mark.get("execution_side") or "SELL").upper()
        vwap = number("full_depth_vwap", mark.get("full_depth_vwap"), minimum=0.0)
        gross = number("gross_value", mark.get("gross_executable_liquidation_value"), minimum=0.0)
        fee = number("exit_fee", mark.get("exit_fee"), minimum=0.0)
        slippage = number("slippage_haircut", mark.get("slippage_haircut"), minimum=0.0)
        net = number("net_value", mark.get("net_executable_liquidation_value"), minimum=0.0)
        if not 0.0 < vwap < 1.0:
            raise MakerCutoverError("maker_mark_price_invalid")
        expected_gross = shares * vwap
        if method == "COMPLEMENT_BUY_AND_MERGE":
            if execution_side != "BUY" or not execution_token or execution_token == token_id:
                raise MakerCutoverError("maker_complement_execution_identity_invalid")
            expected_gross = shares * (1.0 - vwap)
        elif method == "DIRECT_SELL":
            if execution_side != "SELL" or execution_token != token_id:
                raise MakerCutoverError("maker_direct_execution_identity_invalid")
        else:
            raise MakerCutoverError("maker_liquidation_method_invalid")
        if abs(gross - expected_gross) > 1e-7:
            raise MakerCutoverError("maker_mark_gross_mismatch")
        if abs(net - max(0.0, gross - fee - slippage)) > 1e-7:
            raise MakerCutoverError("maker_mark_net_mismatch")
        exchange_ms = int(number("exchange_ts_ms", mark.get("exchange_ts_ms"), minimum=1.0))
        receive_ms = int(number("receive_ts_ms", mark.get("receive_ts_ms"), minimum=1.0))
        snapshot_id = str(mark.get("book_snapshot_id") or "")
        fee_source = str(mark.get("exit_fee_source") or "")
        if exchange_ms > receive_ms or receive_ms > status_ms or not snapshot_id or not fee_source:
            raise MakerCutoverError("maker_mark_causality_invalid")
        order_id = f"maker-cutover-order-{stable_id(nonce, market_id, token_id)}"
        fill_id = f"maker-cutover-fill-{stable_id(order_id, execution_token, snapshot_id)}"
        position_id = f"maker-position-{stable_id(model_sha, market_id, token_id)}"
        pnl = net - cost
        common = dict(
            strategy=STRATEGY, model_sha=model_sha, model_version="cutover-liquidation-v1",
            order_id=order_id, fill_id=fill_id, position_id=position_id,
            market_id=market_id, event_id=condition_id or None, token_id=execution_token,
            side=execution_side, recorded_ts_ms=current_ms,
        )
        events.append(LedgerEvent(
            event_type="FILL", record_id=stable_id("FILL", nonce, market_id, token_id),
            exchange_ts_ms=exchange_ms, receive_ts_ms=receive_ms, book_snapshot_id=snapshot_id,
            fill_price=vwap, filled_size=shares, complete=True, fee=fee,
            fee_source=fee_source, slippage=slippage,
            executable_liquidation_value=net,
            metadata={"purpose": "LIQUIDATION", "cutover": True, "nonce": nonce,
                      "full_visible_depth": True, "paper_tif": "FAK",
                      "liquidation_method": method, "inventory_token_id": token_id,
                      "complete_set_merge": method == "COMPLEMENT_BUY_AND_MERGE"}, **common,
        ))
        events.append(LedgerEvent(
            event_type="FINAL", record_id=stable_id("FINAL", nonce, market_id, token_id),
            final_pnl=pnl, realized_cashflow=net, unwind_loss=slippage,
            capital_cost=0.0, latency_cost=0.0,
            metadata={
                "purpose": "LIQUIDATION", "cutover": True, "nonce": nonce, "realized": True,
                "liquidation_method": method, "inventory_token_id": token_id,
                "complete_set_merge": method == "COMPLEMENT_BUY_AND_MERGE",
                "cost_vector_complete": True, "unwind_accounted": True,
                "pnl_decomposition": {"gross_exit_value": gross, "entry_cost": cost,
                                      "exit_fee": fee, "slippage_haircut": slippage},
            }, **common,
        ))
        updated_inventory[market_id][f"{outcome}_shares"] = 0.0
        updated_inventory[market_id][f"{outcome}_cost"] = 0.0
        total_net += net
        total_pnl += pnl
        liquidations.append({"market_id": market_id, "token_id": token_id,
                             "execution_token_id": execution_token, "execution_side": execution_side,
                             "liquidation_method": method, "shares": shares,
                             "net_cashflow": net, "final_pnl": pnl})
    if by_identity:
        raise MakerCutoverError("maker_inventory_mark_missing")

    # First drain any events produced immediately before shutdown.  The
    # liquidation itself is journaled before its first externally visible write.
    reconciliation = reconcile_invalid_spool(root, model_sha, nonce)
    before = drain_spool(root, model_sha=model_sha, writer_id=f"cutover-pre-{nonce}")
    if before["rejected"]:
        raise MakerCutoverError("pre_cutover_spool_rejected")

    updated["cash"] = number("state_cash", state.get("cash"), minimum=0.0) + total_net
    updated["realized_trading_pnl"] = number(
        "realized_trading_pnl", state.get("realized_trading_pnl") or 0.0
    ) + total_pnl
    updated["timestamp_ms"] = current_ms
    updated["cutover_liquidation_nonce"] = nonce
    final_status = copy.deepcopy(status)
    final_status.update({
        "timestamp_ms": current_ms, "cash": updated["cash"], "equity": updated["cash"],
        "executable_inventory_value": 0.0, "gross_exit_fees": 0.0,
        "liquidation_slippage_haircut": 0.0, "positions": [], "unmarkable_tokens": [],
        "marking_complete": True, "new_risk_frozen": True, "drain_requested": True,
        "drain_complete": True, "degraded": False,
        "realized_trading_pnl": updated["realized_trading_pnl"],
        "source": "verified_cutover_full_depth_liquidation",
    })
    journal = {
        "schema": "polymarket_v7_maker_cutover_liquidation_v1", "state": "LIQUIDATION_PENDING",
        "timestamp_ms": current_ms, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "model_sha": model_sha, "nonce": nonce,
        "positions_liquidated": len(liquidations), "net_cashflow": total_net,
        "final_pnl": total_pnl, "liquidations": liquidations,
        "rejected_spool_records": reconciliation["rejected_count"],
        "ledger_record_ids": [event.record_id for event in events],
        "original_state_digest": object_digest(state),
        "final_state_digest": object_digest(updated),
        "events": [event.to_dict() for event in events],
        "updated_state": updated,
        "final_status": final_status,
    }
    atomic_json(receipt_path, journal)
    return resume_transaction(root, model_sha, nonce, journal, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--mark", type=Path)
    args = parser.parse_args()
    print(json.dumps(finalize(
        args.run_root, args.model_sha, args.nonce, mark_path=args.mark,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
