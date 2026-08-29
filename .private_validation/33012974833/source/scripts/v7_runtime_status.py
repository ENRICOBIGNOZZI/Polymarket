#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import threading
import time
from pathlib import Path


def finite(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def last_csv(path: Path) -> dict[str, str]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return rows[-1] if rows else {}
    except OSError:
        return {}


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle) if row]
    except OSError:
        return []


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def fill_counts(path: Path) -> dict[str, int]:
    rows = read_csv(path)
    buy = sell = settle = 0
    for row in rows:
        action = str(row.get("action") or "").upper()
        if action.startswith("BUY"):
            buy += 1
        elif action.startswith("SELL"):
            sell += 1
        elif action.startswith("SETTLE"):
            settle += 1
    return {"fills": len(rows), "buy_fills": buy, "sell_fills": sell, "settle_fills": settle}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate canonical V7 PAPER runtime state")
    parser.add_argument("--config", type=Path, default=Path("config/paper_v7.json"))
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()

    cfg = read_json(args.config)
    v7 = cfg.get("v7") if isinstance(cfg.get("v7"), dict) else {}
    starting = finite(cfg.get("starting_capital"), 10000.0)
    fractions = {
        "micro_maker": finite(v7.get("micro_maker_capital_fraction"), 0.22),
        "micro_taker": finite(v7.get("micro_taker_capital_fraction"), 0.12),
        "relative_value": finite(v7.get("relative_value_capital_fraction"), 0.34),
        "hard_arb": finite(v7.get("hard_arb_capital_fraction"), 0.22),
        "external": finite(v7.get("external_capital_fraction"), 0.08),
    }
    reserve_fraction = finite(v7.get("reserve_fraction"), 0.02)
    if not math.isclose(sum(fractions.values()) + reserve_fraction, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise SystemExit("V7 capital fractions must sum to one")
    allocations = {name: starting * fraction for name, fraction in fractions.items()}
    reserve = starting * reserve_fraction

    maker = last_csv(args.run_root / "maker" / "maker_equity.csv")
    micro = read_json(args.run_root / "micro_taker" / "status.json")
    broker = last_csv(args.run_root / "multileg_equity.csv")
    hard = read_json(args.run_root / "hard_arb" / "status.json")
    external = read_json(args.run_root / "external" / "status.json")

    maker_fills = fill_counts(args.run_root / "maker" / "maker_fills.csv")
    micro_fills = fill_counts(args.run_root / "micro_taker" / "fills.csv")
    hard_fills = fill_counts(args.run_root / "hard_arb" / "fills.csv")
    external_fills = fill_counts(args.run_root / "external" / "fills.csv")

    maker_equity = finite(maker.get("equity"), allocations["micro_maker"])
    micro_equity = finite(micro.get("equity"), allocations["micro_taker"])
    broker_equity = finite(broker.get("equity"), allocations["relative_value"])
    hard_equity = finite(hard.get("equity"), allocations["hard_arb"])
    external_equity = finite(external.get("equity"), allocations["external"])
    equity = reserve + maker_equity + micro_equity + broker_equity + hard_equity + external_equity

    previous = read_json(args.run_root / "runtime_status.json")
    peak = max(starting, finite(previous.get("peak_equity"), starting), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    local_killed = (
        bool(int(finite(maker.get("killed"))))
        or bool(micro.get("killed", False))
        or bool(int(finite(broker.get("killed"))))
        or bool(hard.get("killed", False))
        or bool(external.get("killed", False))
    )
    killed = local_killed or drawdown >= finite(cfg.get("max_drawdown"), 0.15)

    live = {
        "micro_maker": int(finite(maker.get("resting_orders"))) + int(finite(maker.get("positions"))),
        "micro_taker": int(finite(micro.get("open_positions"))),
        "relative_value": int(finite(broker.get("live_bundles"))),
        "hard_arb": int(finite(hard.get("open_positions"))),
        "external": int(finite(external.get("open_positions"))),
    }
    reserved_cash = reserve + finite(maker.get("reserved_cash")) + finite(broker.get("reserved_cash"))
    gross_exposure = (
        finite(maker.get("reserved_cash"))
        + finite(micro.get("gross_exposure"))
        + finite(broker.get("gross_entry_cash"))
        + finite(hard.get("gross_exposure"))
        + finite(external.get("gross_exposure"))
    )

    strategy_equity = {
        "micro_maker": maker_equity,
        "micro_taker": micro_equity,
        "relative_value": broker_equity,
        "hard_arb": hard_equity,
        "external": external_equity,
    }
    fill_map = {
        "micro_maker": maker_fills,
        "micro_taker": micro_fills,
        "relative_value": {"fills": 0, "buy_fills": 0, "sell_fills": 0, "settle_fills": 0},
        "hard_arb": hard_fills,
        "external": external_fills,
    }
    strategy_killed = {
        "micro_maker": bool(int(finite(maker.get("killed")))),
        "micro_taker": bool(micro.get("killed", False)),
        "relative_value": bool(int(finite(broker.get("killed")))),
        "hard_arb": bool(hard.get("killed", False)),
        "external": bool(external.get("killed", False)),
    }
    strategies: dict[str, dict] = {}
    for name in fractions:
        strategies[name] = {
            "equity": strategy_equity[name],
            "pnl": strategy_equity[name] - allocations[name],
            "live_units": live[name],
            "killed": strategy_killed[name],
            **fill_map[name],
        }
    strategies["micro_taker"].update({
        "signals": int(finite(micro.get("signals"))),
        "best_edge": finite(micro.get("best_edge")),
        "labeled_samples": int(finite(micro.get("labeled_samples"))),
    })
    strategies["hard_arb"].update({
        "signals": int(finite(hard.get("positive_candidates"))),
        "best_edge": finite(hard.get("best_edge")),
        "entered": int(finite(hard.get("entered"))),
    })

    cash = (
        finite(maker.get("cash"), allocations["micro_maker"])
        + finite(micro.get("cash"), allocations["micro_taker"])
        + finite(broker.get("cash"), allocations["relative_value"])
        + finite(hard.get("cash"), allocations["hard_arb"])
        + finite(external.get("cash"), allocations["external"])
        + reserve
    )
    status = {
        "schema": "polymarket_v7_runtime_status_v1",
        "timestamp": int(time.time()),
        "version": 7,
        "paper_only": True,
        "authenticated_execution": False,
        "starting_capital": starting,
        "cash": cash,
        "equity": equity,
        "peak_equity": peak,
        "pnl": equity - starting,
        "drawdown": drawdown,
        "killed": killed,
        "live_units": sum(live.values()),
        "reserved_cash": reserved_cash,
        "gross_exposure": gross_exposure,
        "realized_pnl": finite(micro.get("realized_pnl_total")) + finite(hard.get("realized_pnl")) + finite(external.get("realized_pnl")),
        "execution_imbalance": 0.0,
        "execution_staleness": 0.0,
        "strategies": strategies,
        "graph_scan": read_json(args.run_root / "relation_status.json"),
        "graph_joint_state": read_json(args.run_root / "relation_joint_state_guard.json"),
        "external_bridge": read_json(args.run_root / "external_bridge_status.json"),
    }
    atomic_json(args.run_root / "runtime_status.json", status)

    fields = [
        "name", "expert", "capital_fraction", "starting_capital", "cash", "equity", "pnl",
        "realized_pnl", "peak_equity", "drawdown", "gross_exposure", "open_positions", "killed",
        "alive", "status_age_seconds", "restarts", "fills", "buy_fills", "sell_fills", "settle_fills",
    ]
    rows = []
    for name in fractions:
        allocation = allocations[name]
        current_equity = strategy_equity[name]
        counts = fill_map[name]
        rows.append({
            "name": name,
            "expert": name,
            "capital_fraction": fractions[name],
            "starting_capital": allocation,
            "cash": current_equity,
            "equity": current_equity,
            "pnl": current_equity - allocation,
            "realized_pnl": 0.0,
            "peak_equity": max(allocation, current_equity),
            "drawdown": 0.0,
            "gross_exposure": 0.0,
            "open_positions": live[name],
            "killed": 1 if strategy_killed[name] else 0,
            "alive": 1,
            "status_age_seconds": 0,
            "restarts": 0,
            **counts,
        })
    atomic_csv(args.run_root / "strategy_status.csv", fields, rows)
    atomic_json(args.run_root / "allocator_status.json", {
        "schema": "polymarket_v7_allocator_status_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "models_expected": len(fractions),
        "models_alive": len(fractions),
        "reserve_fraction": reserve_fraction,
        "global_max_drawdown": finite(cfg.get("max_drawdown"), 0.15),
        "global_max_gross_fraction": finite(cfg.get("max_gross_fraction"), 1.0),
        "global_gross_fraction": gross_exposure / max(starting, 1.0),
        "timestamp": int(time.time()),
    })
    print(json.dumps({key: status[key] for key in ("equity", "pnl", "drawdown", "live_units", "killed")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
