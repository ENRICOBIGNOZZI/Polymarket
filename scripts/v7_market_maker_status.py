#!/usr/bin/env python3
"""Fail-closed executable equity mark for the V7 professional maker sleeve.

Residual maker inventory is valued only at full visible bid depth, net of the
verified taker fee schedule and an explicit liquidation slippage haircut.  If
market identity, fee provenance, or executable depth is missing, the sleeve is
unmarkable and the account-level guard must fail closed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_market_common import finite, request_json, resolve_fee_details, fee_per_share


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


def assess(
    state_path: Path,
    sleeve_config: Path,
    output: Path,
    *,
    selection_path: Path | None = None,
) -> dict[str, Any]:
    state = read_json(state_path)
    cfg = read_json(sleeve_config)
    v7 = cfg.get("v7") if isinstance(cfg.get("v7"), dict) else {}
    if cfg.get("paper_only") is not True or v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
        raise ValueError("maker_status_requires_paper_auth_disabled")
    if not state:
        report = {
            "schema": "polymarket_v7_professional_maker_status_v1",
            "timestamp_ms": time.time_ns() // 1_000_000,
            "paper_only": True,
            "authenticated_execution": False,
            "equity": float(cfg.get("starting_capital", 0.0)),
            "killed": False,
            "source": "not_started",
            "unmarkable_tokens": [],
        }
        atomic_json(output, report)
        return report
    if state.get("paper_only") is not True or state.get("authenticated_execution") is not False:
        raise ValueError("unsafe_maker_state_contract")

    inventory = state.get("inventory") if isinstance(state.get("inventory"), dict) else {}
    conditions = selection_conditions(selection_path)
    positions: list[tuple[str, str, float]] = []
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
        if yes > 1e-9:
            positions.append((str(market_id), str(row.get("yes_token") or ""), yes))
        if no > 1e-9:
            positions.append((str(market_id), str(row.get("no_token") or ""), no))

    tokens = list(dict.fromkeys(token for _, token, _ in positions if token))
    books: dict[str, dict[str, Any]] = {}
    clob = str(cfg.get("clob_url") or "https://clob.polymarket.com").rstrip("/")
    for start in range(0, len(tokens), 80):
        try:
            rows = request_json(f"{clob}/books", [{"token_id": t} for t in tokens[start:start + 80]])
        except Exception:
            rows = []
        for raw in rows if isinstance(rows, list) else []:
            if isinstance(raw, dict) and raw.get("asset_id"):
                books[str(raw["asset_id"])] = raw

    slippage_bps = max(0.0, finite(cfg.get("slippage_bps"), 0.0))
    liquidation = 0.0
    total_exit_fees = 0.0
    total_slippage_haircut = 0.0
    unmarkable: list[dict[str, Any]] = []
    position_marks: list[dict[str, Any]] = []
    for market_id, token, shares in positions:
        condition_id = conditions.get(market_id, "")
        if not condition_id:
            unmarkable.append({"market_id": market_id, "token_id": token, "shares": shares, "reason": "missing_condition_id"})
            continue
        raw_book = books.get(token)
        if not token or raw_book is None:
            unmarkable.append({"market_id": market_id, "token_id": token, "shares": shares, "reason": "missing_book"})
            continue
        walked = executable_sell_mark(parse_bids(raw_book), shares)
        if walked is None:
            unmarkable.append({"market_id": market_id, "token_id": token, "shares": shares, "reason": "insufficient_bid_depth"})
            continue
        vwap, gross_value = walked
        fees = resolve_fee_details({}, clob, condition_id, token)
        if not fees.verified:
            unmarkable.append({
                "market_id": market_id,
                "condition_id": condition_id,
                "token_id": token,
                "shares": shares,
                "reason": "unverified_exit_fee_schedule",
            })
            continue
        exit_fee = fee_per_share(vwap, fees, taker=True) * shares
        slippage_haircut = gross_value * slippage_bps / 10_000.0
        net_value = max(0.0, gross_value - exit_fee - slippage_haircut)
        liquidation += net_value
        total_exit_fees += exit_fee
        total_slippage_haircut += slippage_haircut
        position_marks.append({
            "market_id": market_id,
            "condition_id": condition_id,
            "token_id": token,
            "shares": shares,
            "full_depth_vwap": vwap,
            "gross_executable_liquidation_value": gross_value,
            "exit_fee": exit_fee,
            "exit_fee_source": fees.source,
            "slippage_haircut": slippage_haircut,
            "net_executable_liquidation_value": net_value,
        })

    cash = finite(state.get("cash"), 0.0)
    equity = cash + liquidation if not unmarkable else 0.0
    starting = max(0.0, finite(state.get("starting_capital"), finite(cfg.get("starting_capital"), 0.0)))
    previous = read_json(output)
    peak = max(starting, finite(previous.get("peak_equity"), starting), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 1.0
    policy_hard = 0.10
    killed = bool(unmarkable) or drawdown >= policy_hard
    report = {
        "schema": "polymarket_v7_professional_maker_status_v1",
        "timestamp_ms": time.time_ns() // 1_000_000,
        "paper_only": True,
        "authenticated_execution": False,
        "model_sha": state.get("model_sha"),
        "cash": cash,
        "gross_exit_fees": total_exit_fees,
        "liquidation_slippage_haircut": total_slippage_haircut,
        "executable_inventory_value": liquidation,
        "equity": equity,
        "peak_equity": peak,
        "drawdown": drawdown,
        "maker_hard_drawdown": policy_hard,
        "killed": killed,
        "source": "full_visible_bid_depth_net_verified_fee_and_slippage" if not unmarkable else "fail_closed_unmarkable",
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = assess(args.state, args.config, args.output, selection_path=args.selection)
    print(json.dumps(report, sort_keys=True))
    return 2 if report.get("killed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
