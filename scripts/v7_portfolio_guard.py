#!/usr/bin/env python3
"""Single account-level PAPER risk guard for the crypto V7 engine."""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any
from collections import Counter, defaultdict

from v7_execution_ledger import LedgerContractError, iter_events


ENGINES = ("CRYPTO_SETTLEMENT_ENGINE",)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(name)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name) from exc
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(name)
    return number


def _native_receipt_valid(event: Any) -> bool:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    receipt = metadata.get("native_settlement_receipt")
    if not isinstance(receipt, dict):
        return False
    client = receipt.get("client_order_id")
    return (
        receipt.get("schema") == "polymarket_v7_native_settlement_receipt_v1"
        and receipt.get("owner") == "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"
        and receipt.get("engine_id") == "CRYPTO_SETTLEMENT_ENGINE"
        and receipt.get("model_sha") == event.model_sha
        and receipt.get("paper_only") is True
        and receipt.get("authenticated_execution") is False
        and receipt.get("real_order_submission") is False
        and receipt.get("real_capital_at_risk") is False
        and receipt.get("execution_mode") == "PAPER_SIMULATED"
        and receipt.get("paper_simulation_authority") is True
        and receipt.get("real_new_risk_authorized") is False
        and receipt.get("single_owner") is True
        and receipt.get("owner_chain") == ["portfolio", "risk", "capital", "oms", "inventory"]
        and isinstance(client, int) and not isinstance(client, bool) and client > 0
        and event.order_id == f"native:{client}"
    )


def _canonical_crypto_equity(run_root: Path, budget: float) -> tuple[float, bool, str, bool, dict[str, Any]] | None:
    runtime = read_json(run_root / "control" / "runtime_status.json")
    ledger = run_root / "ledger" / "execution.jsonl"
    if not runtime or not ledger.is_file():
        return None
    if (
        runtime.get("schema") != "polymarket_v7_runtime_status_v3"
        or runtime.get("paper_only") is not True
        or runtime.get("authenticated_execution") is not False
        or runtime.get("real_order_submission") is not False
    ):
        return 0.0, True, "unsafe_runtime_identity", True, {"reason": "UNSAFE_RUNTIME_IDENTITY"}
    model_sha = str(runtime.get("model_sha") or "")
    try:
        events = [event for event in iter_events(ledger, expected_model_sha=model_sha)
                  if event.strategy == "CRYPTO_SETTLEMENT_ENGINE"]
    except (OSError, LedgerContractError, ValueError, TypeError) as exc:
        return 0.0, True, "canonical_ledger_invalid", True, {"reason": f"{type(exc).__name__}:{exc}"}

    records: set[str] = set()
    fills_seen: set[str] = set()
    coordinator_histories: dict[str, list[Any]] = defaultdict(list)
    native_fills: dict[str, list[Any]] = defaultdict(list)
    native_finals: dict[str, list[Any]] = defaultdict(list)
    components: dict[str, list[float]] = defaultdict(list)

    for event in events:
        if event.record_id in records:
            return 0.0, True, "canonical_ledger_duplicate", True, {"reason": "DUPLICATE_RECORD_ID"}
        records.add(event.record_id)
        if event.event_type not in {"FILL", "FINAL"}:
            continue
        if not event.order_id:
            return 0.0, True, "canonical_ledger_unbound", True, {"reason": "ORDER_ID_MISSING"}

        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        coordinator = metadata.get("coordinator_receipt")
        native = _native_receipt_valid(event)
        if native:
            if not event.market_id:
                return 0.0, True, "canonical_ledger_unbound", True, {"reason": "NATIVE_MARKET_ID_MISSING"}
            if event.event_type == "FILL":
                native_fills[event.market_id].append(event)
            else:
                settlement_id = metadata.get("native_market_settlement_id")
                if settlement_id != f"native-settlement:{event.market_id}":
                    return 0.0, True, "canonical_ledger_unbound", True, {
                        "reason": "NATIVE_FINAL_SETTLEMENT_ID_INVALID", "market_id": event.market_id}
                native_finals[event.market_id].append(event)
        elif isinstance(coordinator, dict) and coordinator:
            coordinator_histories[event.order_id].append(event)
        else:
            return 0.0, True, "canonical_ledger_unbound", True, {"reason": "EXECUTION_RECEIPT_MISSING"}

        if event.event_type == "FILL":
            if not event.fill_id or event.fill_id in fills_seen:
                return 0.0, True, "canonical_ledger_duplicate", True, {"reason": "DUPLICATE_OR_MISSING_FILL_ID"}
            fills_seen.add(event.fill_id)

    realized: list[float] = []
    open_cashflows: list[float] = []
    open_orders = 0
    finals = 0

    # Preserve the existing coordinator-bound accounting contract.
    for order_id, history in coordinator_histories.items():
        order_fills = [event for event in history if event.event_type == "FILL"]
        order_finals = [event for event in history if event.event_type == "FINAL"]
        if len(order_finals) > 1:
            return 0.0, True, "canonical_ledger_duplicate", True, {"reason": "MULTIPLE_FINALS", "order_id": order_id}
        if order_finals:
            if not order_fills:
                return 0.0, True, "canonical_ledger_unbound", True, {"reason": "FINAL_WITHOUT_FILL", "order_id": order_id}
            final = order_finals[0]
            if final.final_pnl is None or not math.isfinite(float(final.final_pnl)):
                return 0.0, True, "canonical_ledger_unmarkable", True, {"reason": "FINAL_PNL_INVALID", "order_id": order_id}
            pnl = float(final.final_pnl)
            realized.append(pnl)
            finals += 1
            component = str(final.metadata.get("component") or final.metadata.get("model_family") or "UNKNOWN")
            components[component].append(pnl)
            continue
        if not order_fills:
            continue
        open_orders += 1
        for fill in order_fills:
            values = (fill.filled_size, fill.fill_price, fill.fee)
            if any(value is None or not math.isfinite(float(value)) or float(value) < 0 for value in values):
                return 0.0, True, "canonical_ledger_unmarkable", True, {"reason": "OPEN_FILL_COST_INVALID", "order_id": order_id}
            if str(fill.side or "").upper() != "BUY":
                return 0.0, True, "canonical_ledger_unsupported", True, {"reason": "OPEN_NONBUY_POSITION", "order_id": order_id}
            open_cashflows.append(-(float(fill.filled_size) * float(fill.fill_price) + float(fill.fee)))

    # Native PAPER markets are settled as one economic unit. Before resolution,
    # value residual inventory at zero and include only observed cashflows. This
    # is conservative and supports inventory-backed SELL fills without double
    # counting the earlier BUY basis.
    native_markets = set(native_fills) | set(native_finals)
    for market_id in native_markets:
        fills = native_fills.get(market_id, [])
        market_finals = native_finals.get(market_id, [])
        if len(market_finals) > 1:
            return 0.0, True, "canonical_ledger_duplicate", True, {
                "reason": "MULTIPLE_NATIVE_MARKET_FINALS", "market_id": market_id}
        if market_finals:
            if not fills:
                return 0.0, True, "canonical_ledger_unbound", True, {
                    "reason": "NATIVE_FINAL_WITHOUT_FILL", "market_id": market_id}
            final = market_finals[0]
            if final.final_pnl is None or not math.isfinite(float(final.final_pnl)):
                return 0.0, True, "canonical_ledger_unmarkable", True, {
                    "reason": "NATIVE_FINAL_PNL_INVALID", "market_id": market_id}
            pnl = float(final.final_pnl)
            realized.append(pnl)
            finals += 1
            component = str(final.metadata.get("component") or "native_market_settlement")
            components[component].append(pnl)
            continue

        inventory: dict[str, float] = defaultdict(float)
        native_open_orders: set[str] = set()
        for fill in fills:
            values = (fill.filled_size, fill.fill_price, fill.fee)
            if any(value is None or not math.isfinite(float(value)) or float(value) < 0 for value in values):
                return 0.0, True, "canonical_ledger_unmarkable", True, {
                    "reason": "NATIVE_OPEN_FILL_INVALID", "market_id": market_id}
            if not fill.token_id or fill.side not in {"BUY", "SELL"}:
                return 0.0, True, "canonical_ledger_unmarkable", True, {
                    "reason": "NATIVE_OPEN_FILL_INSTRUMENT_INVALID", "market_id": market_id}
            qty = float(fill.filled_size)
            price = float(fill.fill_price)
            fee = float(fill.fee)
            if qty <= 0 or not (0.0 <= price <= 1.0):
                return 0.0, True, "canonical_ledger_unmarkable", True, {
                    "reason": "NATIVE_OPEN_FILL_ECONOMICS_INVALID", "market_id": market_id}
            if fill.side == "BUY":
                inventory[fill.token_id] += qty
                open_cashflows.append(-(qty * price + fee))
            else:
                inventory[fill.token_id] -= qty
                if inventory[fill.token_id] < -1e-9:
                    return 0.0, True, "canonical_ledger_unsupported", True, {
                        "reason": "NATIVE_NAKED_SELL", "market_id": market_id}
                open_cashflows.append(qty * price - fee)
            native_open_orders.add(fill.order_id)
        open_orders += len(native_open_orders)

    realized_pnl = math.fsum(realized)
    conservative_open_cashflow = math.fsum(open_cashflows)
    equity = budget + realized_pnl + conservative_open_cashflow
    if not math.isfinite(equity) or equity < -1e-9:
        return 0.0, True, "canonical_ledger_negative_equity", True, {
            "reason": "NEGATIVE_ENGINE_EQUITY", "equity": equity}
    details = {
        "model_sha": model_sha,
        "ledger_records": len(events),
        "final_count": finals,
        "open_order_count": open_orders,
        "realized_pnl": realized_pnl,
        "conservative_open_cashflow": conservative_open_cashflow,
        "component_realized_pnl": {
            key: math.fsum(values) for key, values in sorted(components.items())},
        "open_position_valuation": "ZERO_RECOVERY_CONSERVATIVE_UNTIL_FINAL",
        "receipt_modes": ["COORDINATOR", "NATIVE_PAPER_SINGLE_OWNER"],
    }
    return max(0.0, equity), False, "canonical_ledger_conservative", False, details


def engine_equity(
    run_root: Path, engine_id: str, budget: float,
) -> tuple[float, bool, str, bool, dict[str, Any]]:
    if engine_id == "CRYPTO_SETTLEMENT_ENGINE":
        canonical = _canonical_crypto_equity(run_root, budget)
        if canonical is not None:
            return canonical
        state = read_json(run_root / "external_fair" / "paper_router_status.json")
        key = "equity"
    else:
        return 0.0, True, "unknown_engine", True, {"reason": "UNKNOWN_ENGINE"}
    if not state:
        return budget, False, "not_started", False, {}
    if (
        state.get("paper_only") is not True
        or state.get("authenticated_execution") is not False
        or state.get("real_order_submission") not in (None, False)
    ):
        return 0.0, True, "unsafe_state_contract", True, {"reason": "UNSAFE_STATE_CONTRACT"}
    try:
        value = _finite_nonnegative(state[key], "unmarkable_equity")
    except (KeyError, ValueError):
        return 0.0, True, "unmarkable_equity", True, {"reason": "UNMARKABLE_EQUITY"}
    source = str(state.get("source") or "reported")
    fatal = source in {"fail_closed_unmarkable", "unsafe_state_contract"}
    return value, bool(state.get("killed")), source, fatal, {}


def assess(run_root: Path, allocation_manifest: Path, *, max_drawdown: float) -> dict[str, Any]:
    manifest = read_json(allocation_manifest)
    budgets = manifest.get("engine_budgets") if isinstance(manifest.get("engine_budgets"), dict) else {}
    account = _finite_nonnegative(manifest.get("account_starting_capital"), "account")
    reserve = _finite_nonnegative(manifest.get("reserve_budget"), "reserve")
    if (
        manifest.get("schema") != "polymarket_v7_capital_allocation_v3"
        or manifest.get("paper_only") is not True
        or manifest.get("authenticated_execution") is not False
        or manifest.get("real_order_submission") is not False
        or manifest.get("capital_authority_owner") != "V7_CANONICAL_ALLOCATOR"
        or manifest.get("capital_authority_owner_count") != 1
        or set(budgets) != set(ENGINES)
        or account <= 0.0
    ):
        raise ValueError("valid_engine_allocation_manifest_required")
    if abs(sum(_finite_nonnegative(v, "engine_budget") for v in budgets.values()) + reserve - account) > 1e-6:
        raise ValueError("engine_allocation_sum_mismatch")
    states: dict[str, Any] = {}
    equity = reserve
    fatal_state = False
    locally_killed: list[str] = []
    fatal_engines: list[str] = []
    for engine_id in ENGINES:
        budget = _finite_nonnegative(budgets[engine_id], "engine_budget")
        value, killed, source, fatal, details = engine_equity(run_root, engine_id, budget)
        equity += value
        if killed:
            locally_killed.append(engine_id)
        if fatal:
            fatal_engines.append(engine_id)
        fatal_state = fatal_state or fatal
        states[engine_id] = {
            "budget": budget, "equity": value, "source": source,
            "killed": killed, "fatal_to_portfolio": fatal,
            "details": details,
        }
    previous = read_json(run_root / "control" / "portfolio_state.json")
    peak = max(account, float(previous.get("peak", account)), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 1.0
    killed = fatal_state or drawdown >= max(0.0, min(1.0, float(max_drawdown)))
    report = {
        "schema": "polymarket_v7_portfolio_guard_v2",
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "risk_owner": "V7_CANONICAL_RISK",
        "account_starting_capital": account,
        "equity": equity,
        "peak": peak,
        "drawdown": drawdown,
        "max_drawdown": max_drawdown,
        "killed": killed,
        "locally_killed_engines": locally_killed,
        "fatal_engines": fatal_engines,
        "engines": states,
    }
    atomic_json(run_root / "control" / "portfolio_state.json", report)
    kill_path = run_root / "control" / "KILL"
    if killed:
        kill_path.parent.mkdir(parents=True, exist_ok=True)
        kill_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--allocation-manifest", type=Path, required=True)
    parser.add_argument("--max-drawdown", type=float, default=0.15)
    args = parser.parse_args()
    report = assess(args.run_root, args.allocation_manifest, max_drawdown=args.max_drawdown)
    print(json.dumps(report, sort_keys=True))
    return 2 if report["killed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
