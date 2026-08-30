#!/usr/bin/env python3
"""Conservative executable equity mark for the V7 professional maker sleeve.

Residual maker inventory is valued only at full visible bid depth, net of the
verified taker fee schedule and an explicit liquidation slippage haircut.  If
market identity, fee provenance, or executable depth is missing, that inventory
is valued at zero and new maker risk is frozen. Cash and other verified marks
remain valid; only a real conservative drawdown triggers the global kill path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_market_common import FeeDetails, finite, request_json, resolve_fee_details, fee_per_share


TRUSTED_REGISTRY_FEE_SOURCES = {
    "gamma:feeSchedule",
    "gamma:fees_disabled",
    "clob:fd",
    "clob:fees_disabled",
}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sync_freeze(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    if report.get("new_risk_frozen") is True:
        atomic_json(path, {
            "schema": "polymarket_v7_maker_freeze_v1",
            "timestamp_ms": report.get("timestamp_ms"),
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "model_sha": report.get("model_sha"),
            "reason": "cutover_drain" if report.get("drain_requested") is True else "unmarkable_inventory",
            "unmarkable_tokens": report.get("unmarkable_tokens", []),
        })
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def selection_conditions(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    root = read_json(path)
    rows = root.get("markets") if isinstance(root.get("markets"), list) else []
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        market_id = str(row.get("market_id") or "")
        condition_id = str(row.get("condition_id") or "")
        if market_id and condition_id:
            out[market_id] = condition_id
    return out


def registry_fees(
    path: Path | None,
    expected_model_sha: str,
    *,
    now_ms: int | None = None,
) -> dict[str, tuple[FeeDetails, frozenset[str]]]:
    """Return only fresh, exact-SHA and explicitly verified fee evidence.

    A malformed, stale, unsafe or ambiguous registry is equivalent to no
    registry.  The caller may still query the authoritative CLOB descriptor,
    but never invents a zero-fee schedule.
    """
    if path is None or not expected_model_sha:
        return {}
    root = read_json(path)
    if (
        root.get("schema") != "polymarket_v7_fee_reward_registry_v1"
        or root.get("model_sha") != expected_model_sha
        or root.get("paper_only") is not True
        or root.get("authenticated_execution") is not False
        or root.get("real_order_submission") is not False
        or root.get("execution_authority") is not False
        or root.get("unknown_fee_policy") != "NON_EXECUTABLE"
        or root.get("unknown_reward_policy") != "ZERO_EXPECTED_VALUE"
        or root.get("automatic_promotion") is not False
    ):
        return {}
    now = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    result: dict[str, tuple[FeeDetails, frozenset[str]]] = {}
    ambiguous: set[str] = set()
    rows = root.get("markets") if isinstance(root.get("markets"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        condition_id = str(row.get("condition_id") or "")
        fee = row.get("fee") if isinstance(row.get("fee"), dict) else {}
        source = str(fee.get("source") or "")
        rate = finite(fee.get("rate"), math.nan)
        exponent = finite(fee.get("exponent"), math.nan)
        observed_at_ms = int(finite(fee.get("observed_at_ms"), 0.0))
        expires_at_ms = int(finite(fee.get("expires_at_ms"), 0.0))
        enabled = fee.get("enabled")
        taker_only = fee.get("taker_only")
        valid = (
            bool(condition_id)
            and fee.get("verified") is True
            and source in TRUSTED_REGISTRY_FEE_SOURCES
            and math.isfinite(rate) and rate >= 0.0
            and math.isfinite(exponent) and exponent >= 0.0
            and isinstance(taker_only, bool)
            and isinstance(enabled, bool)
            and ((enabled and rate > 0.0) or (not enabled and rate == 0.0))
            and 0 < observed_at_ms <= now <= expires_at_ms
        )
        if not valid:
            continue
        if condition_id in result:
            ambiguous.add(condition_id)
            continue
        tokens = row.get("token_ids") if isinstance(row.get("token_ids"), list) else []
        result[condition_id] = (
            FeeDetails(rate, exponent, taker_only, True, source),
            frozenset(str(token) for token in tokens if str(token)),
        )
    for condition_id in ambiguous:
        result.pop(condition_id, None)
    return result


def parse_bids(raw: dict[str, Any]) -> list[tuple[float, float]]:
    out = []
    for row in raw.get("bids", []) if isinstance(raw.get("bids"), list) else []:
        if not isinstance(row, dict):
            continue
        px, qty = finite(row.get("price"), math.nan), max(0.0, finite(row.get("size"), 0.0))
        if math.isfinite(px) and 0.0 < px < 1.0 and qty > 0.0:
            out.append((px, qty))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def parse_asks(raw: dict[str, Any]) -> list[tuple[float, float]]:
    out = []
    for row in raw.get("asks", []) if isinstance(raw.get("asks"), list) else []:
        if not isinstance(row, dict):
            continue
        px, qty = finite(row.get("price"), math.nan), max(0.0, finite(row.get("size"), 0.0))
        if math.isfinite(px) and 0.0 < px < 1.0 and qty > 0.0:
            out.append((px, qty))
    out.sort(key=lambda x: x[0])
    return out


def executable_sell_mark(levels: list[tuple[float, float]], shares: float) -> tuple[float, float] | None:
    remaining = max(0.0, float(shares))
    value = 0.0
    filled = 0.0
    for price, quantity in levels:
        take = min(remaining, quantity)
        value += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if shares <= 1e-9:
        return 0.0, 0.0
    if remaining > 1e-9 or filled <= 0.0:
        return None
    return value / filled, value


def executable_buy_mark(levels: list[tuple[float, float]], shares: float) -> tuple[float, float] | None:
    return executable_sell_mark(levels, shares)


def assess(
    state_path: Path,
    sleeve_config: Path,
    output: Path,
    *,
    selection_path: Path | None = None,
    fee_registry_path: Path | None = None,
    cutover_zero_recovery: bool = False,
) -> dict[str, Any]:
    state = read_json(state_path)
    drain_path = state_path.parent.parent / "control" / "CUTOVER_DRAIN"
    drain = read_json(drain_path)
    drain_requested = drain_path.exists()
    cfg = read_json(sleeve_config)
    selection = read_json(selection_path) if selection_path is not None else {}
    v7 = cfg.get("v7") if isinstance(cfg.get("v7"), dict) else {}
    if cfg.get("paper_only") is not True or v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
        raise ValueError("maker_status_requires_paper_auth_disabled")
    if not state:
        report = {
            "schema": "polymarket_v7_professional_maker_status_v1",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            # During bootstrap the C++ state does not exist yet.  The pinned
            # selection is already exact-SHA bound and is therefore the
            # authoritative identity until the first maker state arrives.
            "model_sha": selection.get("model_sha"),
            "equity": float(cfg.get("starting_capital", 0.0)),
            "marking_complete": True,
            "new_risk_frozen": drain_requested,
            "drain_requested": drain_requested,
            "drain_complete": drain_requested,
            "degraded": False,
            "killed": False,
            "source": "not_started",
            "unmarkable_tokens": [],
        }
        atomic_json(output, report)
        return report
    if state.get("paper_only") is not True or state.get("authenticated_execution") is not False:
        raise ValueError("unsafe_maker_state_contract")

    expected_model_sha = str(state.get("model_sha") or selection.get("model_sha") or "")
    registered_fees = registry_fees(fee_registry_path, expected_model_sha)

    inventory = state.get("inventory") if isinstance(state.get("inventory"), dict) else {}
    conditions = selection_conditions(selection_path)
    positions: list[tuple[str, str, str, float]] = []
    for market_id, row in inventory.items():
        if not isinstance(row, dict):
            continue
        # The state snapshot is the immutable source of identity for held
        # inventory.  Current reward selection is only a backward-compatible
        # fallback because it can legitimately rotate a filled market out.
        condition_id = str(row.get("condition_id") or "")
        if condition_id:
            conditions[str(market_id)] = condition_id
        yes = max(0.0, finite(row.get("yes_shares"), 0.0))
        no = max(0.0, finite(row.get("no_shares"), 0.0))
        yes_token = str(row.get("yes_token") or "")
        no_token = str(row.get("no_token") or "")
        if yes > 1e-9:
            positions.append((str(market_id), yes_token, no_token, yes))
        if no > 1e-9:
            positions.append((str(market_id), no_token, yes_token, no))

    tokens = list(dict.fromkeys(
        token for _, held_token, complement_token, _ in positions
        for token in (held_token, complement_token) if token
    ))
    books: dict[str, dict[str, Any]] = {}
    book_clocks: dict[str, tuple[int, int, str]] = {}
    clob = str(cfg.get("clob_url") or "https://clob.polymarket.com").rstrip("/")
    for start in range(0, len(tokens), 80):
        try:
            rows = request_json(f"{clob}/books", [{"token_id": t} for t in tokens[start:start + 80]])
        except Exception:
            rows = []
        receive_ms = time.time_ns() // 1_000_000
        for raw in rows if isinstance(rows, list) else []:
            if isinstance(raw, dict) and raw.get("asset_id"):
                token = str(raw["asset_id"])
                books[token] = raw
                exchange_ms = int(finite(raw.get("timestamp"), 0.0))
                if 0 < exchange_ms < 10_000_000_000:
                    exchange_ms *= 1000
                exchange_ms = min(exchange_ms, receive_ms) if exchange_ms > 0 else 0
                snapshot_id = str(raw.get("hash") or "") or hashlib.sha256(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                book_clocks[token] = (exchange_ms, receive_ms, snapshot_id)

    slippage_bps = max(0.0, finite(cfg.get("slippage_bps"), 0.0))
    liquidation = 0.0
    total_exit_fees = 0.0
    total_slippage_haircut = 0.0
    unmarkable: list[dict[str, Any]] = []
    position_marks: list[dict[str, Any]] = []
    for market_id, token, complement_token, shares in positions:
        condition_id = conditions.get(market_id, "")
        if not condition_id:
            unmarkable.append({"market_id": market_id, "token_id": token, "shares": shares, "reason": "missing_condition_id"})
            continue
        if not token or not complement_token:
            unmarkable.append({"market_id": market_id, "token_id": token, "shares": shares, "reason": "missing_binary_token_identity"})
            continue
        routes: list[dict[str, Any]] = []
        direct = executable_sell_mark(parse_bids(books.get(token, {})), shares)
        if direct is not None:
            vwap, transaction_value = direct
            routes.append({
                "method": "DIRECT_SELL", "execution_token": token, "execution_side": "SELL",
                "vwap": vwap, "gross_value": transaction_value, "slippage_base": transaction_value,
            })
        complement = executable_buy_mark(parse_asks(books.get(complement_token, {})), shares)
        if complement is not None:
            vwap, transaction_value = complement
            routes.append({
                "method": "COMPLEMENT_BUY_AND_MERGE", "execution_token": complement_token,
                "execution_side": "BUY", "vwap": vwap,
                "gross_value": max(0.0, shares - transaction_value),
                "slippage_base": transaction_value,
            })
        if not routes:
            unmarkable.append({
                "market_id": market_id, "token_id": token,
                "complement_token_id": complement_token, "shares": shares,
                "reason": "insufficient_direct_and_complement_depth",
            })
            continue

        executable_routes: list[dict[str, Any]] = []
        route_failures: list[str] = []
        for route in routes:
            if route["gross_value"] <= 0.0:
                route_failures.append("nonpositive_liquidation_value")
                continue
            execution_token = route["execution_token"]
            registered = registered_fees.get(condition_id)
            if registered is not None and execution_token in registered[1]:
                fees = registered[0]
            else:
                fees = resolve_fee_details({}, clob, condition_id, execution_token)
            if not fees.verified:
                route_failures.append("unverified_exit_fee_schedule")
                continue
            exchange_ms, receive_ms, snapshot_id = book_clocks.get(execution_token, (0, 0, ""))
            if exchange_ms <= 0 or receive_ms <= 0 or not snapshot_id:
                route_failures.append("missing_causal_book_clock")
                continue
            route["exit_fee"] = fee_per_share(route["vwap"], fees, taker=True) * shares
            route["exit_fee_source"] = fees.source
            route["slippage_haircut"] = route["slippage_base"] * slippage_bps / 10_000.0
            route["net_value"] = max(
                0.0, route["gross_value"] - route["exit_fee"] - route["slippage_haircut"]
            )
            route["exchange_ms"] = exchange_ms
            route["receive_ms"] = receive_ms
            route["snapshot_id"] = snapshot_id
            executable_routes.append(route)
        if not executable_routes:
            unmarkable.append({
                "market_id": market_id, "condition_id": condition_id,
                "token_id": token, "shares": shares,
                "reason": route_failures[0] if route_failures else "no_verified_causal_liquidation_route",
            })
            continue
        best = max(executable_routes, key=lambda route: route["net_value"])
        method = best["method"]
        execution_token = best["execution_token"]
        execution_side = best["execution_side"]
        vwap = best["vwap"]
        gross_value = best["gross_value"]
        exit_fee = best["exit_fee"]
        slippage_haircut = best["slippage_haircut"]
        net_value = best["net_value"]
        exchange_ms = best["exchange_ms"]
        receive_ms = best["receive_ms"]
        snapshot_id = best["snapshot_id"]
        liquidation += net_value
        total_exit_fees += exit_fee
        total_slippage_haircut += slippage_haircut
        position_marks.append({
            "market_id": market_id,
            "condition_id": condition_id,
            "token_id": token,
            "execution_token_id": execution_token,
            "execution_side": execution_side,
            "liquidation_method": method,
            "shares": shares,
            "full_depth_vwap": vwap,
            "gross_executable_liquidation_value": gross_value,
            "exit_fee": exit_fee,
            "exit_fee_source": best["exit_fee_source"],
            "slippage_haircut": slippage_haircut,
            "net_executable_liquidation_value": net_value,
            "exchange_ts_ms": exchange_ms,
            "receive_ts_ms": receive_ms,
            "book_snapshot_id": snapshot_id,
        })

    zero_recovery_writeoffs = 0
    if cutover_zero_recovery and drain_requested and unmarkable:
        if drain.get("schema") != "polymarket_v7_cutover_drain_v1" \
                or drain.get("paper_only") is not True \
                or drain.get("authenticated_execution") is not False \
                or drain.get("real_order_submission") is not False:
            raise ValueError("unsafe_cutover_zero_recovery_authority")
        # This is not an invented executable mark. A PAPER-only exact-SHA
        # cutover may terminally write off an otherwise unmarkable position at
        # zero recovery, realizing the entire durable entry cost as a loss.
        # The original reason remains attached for audit and model evaluation.
        for row in unmarkable:
            position_marks.append({
                "market_id": str(row.get("market_id") or ""),
                "condition_id": str(row.get("condition_id") or ""),
                "token_id": str(row.get("token_id") or ""),
                "execution_token_id": str(row.get("token_id") or ""),
                "execution_side": "WRITE_OFF",
                "liquidation_method": "CONSERVATIVE_ZERO_RECOVERY_WRITE_OFF",
                "shares": max(0.0, finite(row.get("shares"), 0.0)),
                "full_depth_vwap": 0.0,
                "gross_executable_liquidation_value": 0.0,
                "exit_fee": 0.0,
                "exit_fee_source": "paper-cutover-zero-recovery-v1",
                "slippage_haircut": 0.0,
                "net_executable_liquidation_value": 0.0,
                "valuation_policy": "PAPER_CUTOVER_ZERO_RECOVERY",
                "unmarkable_reason": str(row.get("reason") or "unknown"),
            })
        zero_recovery_writeoffs = len(unmarkable)
        unmarkable = []

    cash = finite(state.get("cash"), 0.0)
    # Unmarkable positions contribute zero, but never erase valid cash or
    # independently verified executable inventory marks.
    equity = cash + liquidation
    starting = max(0.0, finite(state.get("starting_capital"), finite(cfg.get("starting_capital"), 0.0)))
    previous = read_json(output)
    peak = max(starting, finite(previous.get("peak_equity"), starting), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 1.0
    policy_hard = 0.10
    new_risk_frozen = bool(unmarkable) or drain_requested
    killed = drawdown >= policy_hard
    report = {
        "schema": "polymarket_v7_professional_maker_status_v1",
        "timestamp_ms": time.time_ns() // 1_000_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": state.get("model_sha"),
        "cash": cash,
        "gross_exit_fees": total_exit_fees,
        "liquidation_slippage_haircut": total_slippage_haircut,
        "executable_inventory_value": liquidation,
        "equity": equity,
        "peak_equity": peak,
        "drawdown": drawdown,
        "maker_hard_drawdown": policy_hard,
        "marking_complete": not unmarkable,
        "new_risk_frozen": new_risk_frozen,
        "drain_requested": drain_requested,
        "drain_complete": drain_requested and not positions,
        "degraded": new_risk_frozen,
        "killed": killed,
        "source": (
            "paper_cutover_conservative_zero_recovery"
            if zero_recovery_writeoffs
            else "full_visible_bid_depth_net_verified_fee_and_slippage"
            if not unmarkable else "fail_closed_unmarkable"
        ),
        "zero_recovery_writeoffs": zero_recovery_writeoffs,
        "positions": position_marks,
        "unmarkable_tokens": unmarkable,
        "realized_trading_pnl": finite(state.get("realized_trading_pnl"), 0.0),
        "estimated_maker_rebate_pnl": finite(state.get("estimated_maker_rebate_pnl"), 0.0),
        "estimated_liquidity_reward_pnl": finite(state.get("estimated_liquidity_reward_pnl"), 0.0),
    }
    atomic_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--fee-registry", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--freeze-path", type=Path)
    parser.add_argument("--cutover-zero-recovery", action="store_true")
    args = parser.parse_args()
    report = assess(
        args.state,
        args.config,
        args.output,
        selection_path=args.selection,
        fee_registry_path=args.fee_registry,
        cutover_zero_recovery=args.cutover_zero_recovery,
    )
    sync_freeze(args.freeze_path, report)
    print(json.dumps(report, sort_keys=True))
    return 2 if report.get("killed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
