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


def _compatibility_views(
    engine_rows: dict[str, dict[str, Any]], reserve: float,
) -> dict[str, dict[str, Any]]:
    """Temporary drain adapter; never grants component capital authority."""
    rows = {
        "external": dict(engine_rows["CRYPTO_SETTLEMENT_ENGINE"]),
        "hard_arb": dict(engine_rows["STRUCTURAL_ARB_ENGINE"]),
        "reserve": {
            "budget": reserve, "equity": reserve, "source": "reserve",
            "killed": False, "fatal_to_portfolio": False,
        },
    }
    for name in ("micro_maker", "fast_structural"):
        rows[name] = {
            "budget": 0.0, "equity": 0.0,
            "source": "zero_authority_budget", "killed": False,
            "fatal_to_portfolio": False,
        }
    return rows


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
        equity += value
        if killed:
            locally_killed.append(engine_id)
        if fatal:
            fatal_engines.append(engine_id)
        fatal_state = fatal_state or fatal
        states[engine_id] = {
            "budget": budget, "equity": value, "source": source,
            "killed": killed, "fatal_to_portfolio": fatal,
        }
    previous = read_json(run_root / "control" / "portfolio_state.json")
    peak = max(account, float(previous.get("peak", account)), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 1.0
    killed = fatal_state or drawdown >= max(0.0, min(1.0, float(max_drawdown)))
    compatibility = _compatibility_views(states, reserve)
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
        "temporary_component_drain_views": compatibility,
        # Temporary compatibility fields are consumed only by the pre-v3
        # cutover archiver. They grant no authority and have an explicit gate.
        "sleeves": compatibility,
        "locally_killed_sleeves": [],
        "fatal_sleeves": [],
        "temporary_compatibility_deletion_gate": "CUTOVER_ARCHIVER_V3_PROVEN",
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
