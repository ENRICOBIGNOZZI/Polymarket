#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


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


def sleeve_equity(run_root: Path, sleeve: str, budget: float) -> tuple[float, bool, str]:
    if sleeve == "graph_rv":
        state = read_json(run_root / "graph_rv" / "status.json")
        key = "equity"
    elif sleeve == "hard_arb":
        state = read_json(run_root / "hard_arb" / "status.json")
        key = "equity_cost_basis"
    elif sleeve == "micro_taker":
        state = read_json(run_root / "micro_taker" / "status.json")
        key = "equity"
    elif sleeve == "micro_maker":
        state = read_json(run_root / "micro_maker" / "status.json")
        key = "equity"
    else:
        return budget, False, "inactive_reserved"
    if not state:
        return budget, False, "not_started"
    if state.get("paper_only") is not True or state.get("authenticated_execution") is not False:
        return 0.0, True, "unsafe_state_contract"
    try:
        value = float(state[key])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0, True, "unmarkable_equity"
    if value < 0:
        return 0.0, True, "negative_equity"
    return value, bool(state.get("killed")), "reported"


def assess(run_root: Path, allocation_manifest: Path, *, max_drawdown: float) -> dict[str, Any]:
    manifest = read_json(allocation_manifest)
    budgets = manifest.get("budgets") if isinstance(manifest.get("budgets"), dict) else {}
    account = float(manifest.get("account_starting_capital", 0.0))
    if account <= 0 or not budgets:
        raise ValueError("valid_allocation_manifest_required")
    states: dict[str, Any] = {}
    equity = 0.0
    killed = False
    for sleeve, raw_budget in budgets.items():
        budget = float(raw_budget)
        if sleeve == "reserve":
            value, sleeve_killed, source = budget, False, "reserve"
        else:
            value, sleeve_killed, source = sleeve_equity(run_root, sleeve, budget)
        equity += value
        killed = killed or sleeve_killed
        states[sleeve] = {"budget": budget, "equity": value, "source": source, "killed": sleeve_killed}
    previous = read_json(run_root / "control" / "portfolio_state.json")
    peak = max(account, float(previous.get("peak", account)), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 1.0
    killed = killed or drawdown >= max(0.0, min(1.0, float(max_drawdown)))
    report = {
        "schema": "polymarket_v7_portfolio_guard_v1",
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "account_starting_capital": account,
        "equity": equity,
        "peak": peak,
        "drawdown": drawdown,
        "max_drawdown": max_drawdown,
        "killed": killed,
        "sleeves": states,
    }
    atomic_json(run_root / "control" / "portfolio_state.json", report)
    kill_path = run_root / "control" / "KILL"
    if killed:
        kill_path.parent.mkdir(parents=True, exist_ok=True)
        kill_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 PAPER account-level portfolio drawdown guard")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--allocation-manifest", type=Path, required=True)
    parser.add_argument("--max-drawdown", type=float, default=0.15)
    args = parser.parse_args()
    report = assess(args.run_root, args.allocation_manifest, max_drawdown=args.max_drawdown)
    print(json.dumps(report, sort_keys=True))
    return 2 if report["killed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
