#!/usr/bin/env python3
"""Fail-closed executable equity mark for the V7 professional maker sleeve."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_market_common import finite, request_json


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


def executable_sell_value(levels: list[tuple[float, float]], shares: float) -> float | None:
    remaining = max(0.0, float(shares))
    value = 0.0
    for price, quantity in levels:
        take = min(remaining, quantity)
        value += take * price
        remaining -= take
        if remaining <= 1e-9:
            return value
    return 0.0 if shares <= 1e-9 else None


def assess(state_path: Path, sleeve_config: Path, output: Path) -> dict[str, Any]:
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
    positions: list[tuple[str, str, float]] = []
    for market_id, row in inventory.items():
        if not isinstance(row, dict):
            continue
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

    liquidation = 0.0
    unmarkable: list[dict[str, Any]] = []
    position_marks: list[dict[str, Any]] = []
    for market_id, token, shares in positions:
        if not token or token not in books:
            unmarkable.append({"market_id": market_id, "token_id": token, "shares": shares, "reason": "missing_book"})
            continue
        value = executable_sell_value(parse_bids(books[token]), shares)
        if value is None:
            unmarkable.append({"market_id": market_id, "token_id": token, "shares": shares, "reason": "insufficient_bid_depth"})
            continue
        liquidation += value
        position_marks.append({"market_id": market_id, "token_id": token, "shares": shares, "executable_liquidation_value": value})

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
        "executable_inventory_value": liquidation,
        "equity": equity,
        "peak_equity": peak,
        "drawdown": drawdown,
        "maker_hard_drawdown": policy_hard,
        "killed": killed,
        "source": "full_visible_bid_depth" if not unmarkable else "fail_closed_unmarkable",
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = assess(args.state, args.config, args.output)
    print(json.dumps(report, sort_keys=True))
    return 2 if report.get("killed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
