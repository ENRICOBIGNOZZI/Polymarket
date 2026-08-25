#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from pathlib import Path


def f(value, default=0.0):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def last_csv(path: Path) -> dict[str, str]:
    try:
        with path.open(newline="", encoding="utf-8") as h:
            rows = list(csv.DictReader(h))
        return rows[-1] if rows else {}
    except OSError:
        return {}


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as h:
            return [dict(row) for row in csv.DictReader(h) if row]
    except OSError:
        return []


def read_json(path: Path) -> dict:
    try:
        x = json.loads(path.read_text(encoding="utf-8"))
        return x if isinstance(x, dict) else {}
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


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config/paper_v6.json"))
    ap.add_argument("--run-root", type=Path, required=True)
    args = ap.parse_args()

    cfg = read_json(args.config)
    v = cfg.get("v6") if isinstance(cfg.get("v6"), dict) else {}
    starting = f(cfg.get("starting_capital"), 10000)
    reserve_frac = f(v.get("reserve_fraction"), .05)
    reserve = starting * reserve_frac

    maker = last_csv(args.run_root / "maker" / "maker_equity.csv")
    micro = read_json(args.run_root / "micro_taker" / "status.json")
    broker = last_csv(args.run_root / "multileg_equity.csv")
    hard = read_json(args.run_root / "hard_arb" / "status.json")
    external = read_json(args.run_root / "external" / "status.json")

    maker_fills = fill_counts(args.run_root / "maker" / "maker_fills.csv")
    micro_fills = fill_counts(args.run_root / "micro_taker" / "fills.csv")
    hard_fills = fill_counts(args.run_root / "hard_arb" / "fills.csv")
    external_fills = fill_counts(args.run_root / "external" / "fills.csv")

    alloc = {
        "micro_maker": starting * f(v.get("micro_maker_capital_fraction"), .12),
        "micro_taker": starting * f(v.get("micro_taker_capital_fraction"), .08),
        "relative_value": starting * f(v.get("relative_value_capital_fraction"), .50),
        "hard_arb": starting * f(v.get("hard_arb_capital_fraction"), .15),
        "external": starting * f(v.get("external_capital_fraction"), .10),
    }
    maker_eq = f(maker.get("equity"), alloc["micro_maker"])
    micro_eq = f(micro.get("equity"), alloc["micro_taker"])
    broker_eq = f(broker.get("equity"), alloc["relative_value"])
    hard_eq = f(hard.get("equity"), alloc["hard_arb"])
    external_eq = f(external.get("equity"), alloc["external"])

    equity = reserve + maker_eq + micro_eq + broker_eq + hard_eq + external_eq
    previous = read_json(args.run_root / "runtime_status.json")
    peak = max(starting, f(previous.get("peak_equity"), starting), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    local_killed = (
        bool(int(f(maker.get("killed"))))
        or bool(micro.get("killed", False))
        or bool(int(f(broker.get("killed"))))
        or bool(hard.get("killed", False))
        or bool(external.get("killed", False))
    )
    killed = local_killed or drawdown >= f(cfg.get("max_drawdown"), .15)

    maker_live = int(f(maker.get("resting_orders"))) + int(f(maker.get("positions")))
    micro_live = int(f(micro.get("open_positions")))
    broker_live = int(f(broker.get("live_bundles")))
    hard_live = int(f(hard.get("open_positions")))
    external_live = int(f(external.get("open_positions")))
    reserved = reserve + f(maker.get("reserved_cash")) + f(broker.get("reserved_cash"))
    gross = (
        f(maker.get("reserved_cash"))
        + f(micro.get("gross_exposure"))
        + f(broker.get("gross_entry_cash"))
        + f(hard.get("gross_exposure"))
        + f(external.get("gross_exposure"))
    )

    relations = read_json(args.run_root / "relation_status.json")
    local_factor = read_json(args.run_root / "local_factor_status.json")
    bridge = read_json(args.run_root / "external_bridge_status.json")

    strategies = {
        "micro_maker": {
            "equity": maker_eq,
            "pnl": maker_eq - alloc["micro_maker"],
            "live_units": maker_live,
            "killed": bool(int(f(maker.get("killed")))),
            **maker_fills,
        },
        "micro_taker": {
            "equity": micro_eq,
            "pnl": micro_eq - alloc["micro_taker"],
            "live_units": micro_live,
            "killed": bool(micro.get("killed", False)),
            "signals": int(f(micro.get("signals"))),
            "best_edge": f(micro.get("best_edge")),
            "labeled_samples": int(f(micro.get("labeled_samples"))),
            **micro_fills,
        },
        "relative_value": {
            "equity": broker_eq,
            "pnl": broker_eq - alloc["relative_value"],
            "live_units": broker_live,
            "killed": bool(int(f(broker.get("killed")))),
        },
        "graph_hard": {
            "equity": hard_eq,
            "pnl": hard_eq - alloc["hard_arb"],
            "live_units": hard_live,
            "killed": bool(hard.get("killed", False)),
            "signals": int(f(hard.get("positive_candidates"))),
            "best_edge": f(hard.get("best_edge")),
            "entered": int(f(hard.get("entered"))),
            **hard_fills,
        },
        "external": {
            "equity": external_eq,
            "pnl": external_eq - alloc["external"],
            "live_units": external_live,
            "killed": bool(external.get("killed", False)),
            **external_fills,
        },
    }

    cash = (
        f(maker.get("cash"), alloc["micro_maker"])
        + f(micro.get("cash"), alloc["micro_taker"])
        + f(broker.get("cash"), alloc["relative_value"])
        + f(hard.get("cash"), alloc["hard_arb"])
        + f(external.get("cash"), alloc["external"])
        + reserve
    )
    status = {
        "schema": "polymarket_v6_runtime_status_v1",
        "timestamp": int(time.time()),
        "version": 6,
        "paper_only": True,
        "starting_capital": starting,
        "cash": cash,
        "equity": equity,
        "peak_equity": peak,
        "pnl": equity - starting,
        "drawdown": drawdown,
        "killed": killed,
        "live_units": maker_live + micro_live + broker_live + hard_live + external_live,
        "reserved_cash": reserved,
        "gross_exposure": gross,
        "realized_pnl": f(micro.get("realized_pnl")) + f(hard.get("realized_pnl")) + f(external.get("realized_pnl")),
        "execution_imbalance": 0.0,
        "execution_staleness": 0.0,
        "strategies": strategies,
        "relations": relations,
        "local_factor": local_factor,
        "external_bridge": bridge,
    }
    atomic_json(args.run_root / "runtime_status.json", status)

    # Transitional V5-shaped telemetry only. Unlike the previous compatibility
    # view, fill counts are sourced from the actual V6 ledgers rather than being
    # hard-coded to zero. Relative-value fill events are intentionally not
    # fabricated until that sleeve has a cumulative fill ledger of its own.
    micro_counts = {k: maker_fills[k] + micro_fills[k] for k in maker_fills}
    graph_counts = hard_fills
    zero_counts = {"fills": 0, "buy_fills": 0, "sell_fills": 0, "settle_fills": 0}
    compat = [
        ("micro", "micro", .20, maker_eq + micro_eq, maker_live + micro_live, micro_counts),
        ("pca", "local_factor", .35, broker_eq * (.35 / .50), broker_live, zero_counts),
        ("graph", "graph_structural_hard", .30, broker_eq * (.15 / .50) + hard_eq, broker_live + hard_live, graph_counts),
        ("semantic", "relation_parser", 0.0, 0.0, 0, zero_counts),
        ("external", "external", .10, external_eq, external_live, external_fills),
    ]
    fields = [
        "name", "expert", "capital_fraction", "starting_capital", "cash", "equity", "pnl",
        "realized_pnl", "peak_equity", "drawdown", "gross_exposure", "open_positions", "killed",
        "alive", "status_age_seconds", "restarts", "fills", "buy_fills", "sell_fills", "settle_fills",
    ]
    rows = []
    for name, expert, frac, eq, live, counts in compat:
        s = starting * frac
        rows.append({
            "name": name,
            "expert": expert,
            "capital_fraction": frac,
            "starting_capital": s,
            "cash": eq,
            "equity": eq,
            "pnl": eq - s,
            "realized_pnl": 0.0,
            "peak_equity": max(s, eq),
            "drawdown": 0.0,
            "gross_exposure": 0.0,
            "open_positions": live,
            "killed": 1 if killed else 0,
            "alive": 1,
            "status_age_seconds": 0,
            "restarts": 0,
            **counts,
        })
    atomic_csv(args.run_root / "strategy_status.csv", fields, rows)
    atomic_json(
        args.run_root / "allocator_status.json",
        {
            "schema": "v6_legacy_health_view",
            "paper_only": True,
            "models_expected": 5,
            "models_alive": 5,
            "reserve_fraction": reserve_frac,
            "global_max_drawdown": f(cfg.get("max_drawdown"), .15),
            "global_max_gross_fraction": f(cfg.get("max_gross_fraction"), .45),
            "global_gross_fraction": gross / max(starting, 1.0),
            "timestamp": int(time.time()),
        },
    )
    print(json.dumps({k: status[k] for k in ("equity", "pnl", "drawdown", "live_units", "killed")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
