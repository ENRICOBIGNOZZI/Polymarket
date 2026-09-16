#!/usr/bin/env python3
"""Single account-level PAPER risk guard for the two V7 economic engines."""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any


ENGINES = ("CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE")


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


def _finite_signed(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(name)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(name) from exc
    if not math.isfinite(number):
        raise ValueError(name)
    return number


def lead_lag_equity_adjustment(run_root: Path) -> dict[str, Any]:
    status = read_json(run_root / "research" / "lead_lag_taker_v1" / "status.json")
    state = read_json(run_root / "research" / "lead_lag_taker_v1" / "state.json")
    if not status and not state:
        return {"present": False, "valid": True, "equity_adjustment": 0.0,
                "realized_pnl": 0.0, "open_cost_at_risk": 0.0, "open_positions": 0}
    if not status or not state:
        return {"present": True, "valid": False, "fatal_reason": "lead_lag_status_state_pair_incomplete"}
    try:
        if (status.get("schema") != "polymarket_v7_lead_lag_taker_v1_status"
                or status.get("paper_only") is not True
                or status.get("authenticated_execution") is not False
                or status.get("real_order_submission") is not False
                or status.get("real_capital_at_risk") is not False
                or status.get("model_sha") != state.get("model_sha")
                or status.get("protocol_hash") != state.get("protocol_hash")):
            raise ValueError("lead_lag_identity_or_safety_invalid")
        positions = state.get("positions")
        if not isinstance(positions, dict):
            raise ValueError("lead_lag_positions_missing")
        entries = int(status.get("entries"))
        settled = int(status.get("settled"))
        open_positions = int(status.get("open_positions"))
        if min(entries, settled, open_positions) < 0 or entries != settled + open_positions:
            raise ValueError("lead_lag_status_counts_invalid")
        if entries != len(positions):
            raise ValueError("lead_lag_position_count_mismatch")
        realized = _finite_signed(status.get("realized_pnl"), "lead_lag_realized_pnl")
        state_realized = _finite_signed(state.get("realized_pnl"), "lead_lag_state_realized_pnl")
        if abs(realized - state_realized) > 1e-7 * max(1.0, abs(realized), abs(state_realized)):
            raise ValueError("lead_lag_realized_pnl_mismatch")
        final_sum = 0.0
        open_cost = 0.0
        observed_settled = 0
        observed_open = 0
        for position_id, row in positions.items():
            if not isinstance(position_id, str) or not position_id or not isinstance(row, dict):
                raise ValueError("lead_lag_position_shape_invalid")
            if row.get("protocol_hash") != status.get("protocol_hash"):
                raise ValueError("lead_lag_position_protocol_mismatch")
            if row.get("settled") is True:
                observed_settled += 1
                final_sum += _finite_signed(row.get("final_pnl"), "lead_lag_final_pnl")
            elif row.get("settled") is False:
                observed_open += 1
                open_cost += _finite_nonnegative(row.get("entry_cost"), "lead_lag_entry_cost")
                open_cost += _finite_nonnegative(row.get("entry_fee"), "lead_lag_entry_fee")
            else:
                raise ValueError("lead_lag_position_settlement_state_invalid")
        if observed_settled != settled or observed_open != open_positions:
            raise ValueError("lead_lag_position_status_count_mismatch")
        if abs(final_sum - realized) > 1e-7 * max(1.0, abs(final_sum), abs(realized)):
            raise ValueError("lead_lag_terminal_pnl_sum_mismatch")
        return {
            "present": True, "valid": True, "model_sha": status.get("model_sha"),
            "protocol_hash": status.get("protocol_hash"), "realized_pnl": realized,
            "open_cost_at_risk": open_cost, "open_positions": open_positions,
            "equity_adjustment": realized - open_cost,
            "open_mark_policy": "ZERO_RECOVERY_VALUE_CONSERVATIVE_RISK_MARK_NOT_SETTLEMENT",
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return {"present": True, "valid": False, "fatal_reason": f"{type(exc).__name__}:{exc}"}


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


def engine_equity(
    run_root: Path, engine_id: str, budget: float,
) -> tuple[float, bool, str, bool]:
    if engine_id == "CRYPTO_SETTLEMENT_ENGINE":
        state = read_json(run_root / "external_fair" / "paper_router_status.json")
        key = "equity"
    elif engine_id == "STRUCTURAL_ARB_ENGINE":
        state = read_json(run_root / "hard_arb" / "status.json")
        key = "equity_cost_basis"
    else:
        return 0.0, True, "unknown_engine", True
    if not state:
        return budget, False, "not_started", False
    if (
        state.get("paper_only") is not True
        or state.get("authenticated_execution") is not False
        or state.get("real_order_submission") not in (None, False)
    ):
        return 0.0, True, "unsafe_state_contract", True
    try:
        value = _finite_nonnegative(state[key], "unmarkable_equity")
    except (KeyError, ValueError):
        return 0.0, True, "unmarkable_equity", True
    source = str(state.get("source") or "reported")
    fatal = source in {"fail_closed_unmarkable", "unsafe_state_contract"}
    return value, bool(state.get("killed")), source, fatal


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
        value, killed, source, fatal = engine_equity(run_root, engine_id, budget)
        components: dict[str, Any] = {"base_engine_equity": value}
        if engine_id == "CRYPTO_SETTLEMENT_ENGINE":
            lead_lag = lead_lag_equity_adjustment(run_root)
            components["lead_lag_taker_v1"] = lead_lag
            if lead_lag.get("valid") is not True:
                value = 0.0
                killed = True
                fatal = True
                source = "lead_lag_unreconciled_fail_closed"
            elif lead_lag.get("present") is True:
                value += float(lead_lag["equity_adjustment"])
                source = source + "+lead_lag_conservative_open_mark"
        equity += value
        if killed:
            locally_killed.append(engine_id)
        if fatal:
            fatal_engines.append(engine_id)
        fatal_state = fatal_state or fatal
        states[engine_id] = {
            "budget": budget, "equity": value, "source": source,
            "killed": killed, "fatal_to_portfolio": fatal, "components": components,
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
