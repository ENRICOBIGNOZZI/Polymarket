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
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return rows[-1] if rows else {}
    except OSError:
        return {}


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path("config/paper_v6.json"))
    ap.add_argument("--run-root", type=Path, required=True)
    args = ap.parse_args()
    cfg = read_json(args.config)
    v6 = cfg.get("v6") if isinstance(cfg.get("v6"), dict) else {}
    starting = f(cfg.get("starting_capital"), 10000.0)
    reserve_fraction = f(v6.get("reserve_fraction"), 0.05)
    reserve = starting * reserve_fraction

    maker = last_csv(args.run_root / "maker" / "maker_equity.csv")
    broker = last_csv(args.run_root / "multileg_equity.csv")
    external = read_json(args.run_root / "external" / "status.json")

    maker_start = starting * f(v6.get("micro_capital_fraction"), 0.20)
    broker_start = starting * f(v6.get("multileg_capital_fraction"), 0.65)
    external_start = starting * f(v6.get("external_capital_fraction"), 0.10)
    maker_eq = f(maker.get("equity"), maker_start)
    broker_eq = f(broker.get("equity"), broker_start)
    external_eq = f(external.get("equity"), external_start)
    equity = reserve + maker_eq + broker_eq + external_eq

    previous = read_json(args.run_root / "runtime_status.json")
    peak = max(starting, f(previous.get("peak_equity"), starting), equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0 else 0.0
    local_killed = bool(int(f(maker.get("killed")))) or bool(int(f(broker.get("killed")))) or bool(external.get("killed", False))
    killed = local_killed or drawdown >= f(cfg.get("max_drawdown"), 0.15)

    maker_live = int(f(maker.get("resting_orders"))) + int(f(maker.get("positions")))
    broker_live = int(f(broker.get("live_bundles")))
    external_live = int(f(external.get("open_positions")))
    reserved_cash = reserve + f(maker.get("reserved_cash")) + f(broker.get("reserved_cash"))
    gross = f(broker.get("gross_entry_cash")) + f(external.get("gross_exposure")) + f(maker.get("reserved_cash"))

    relations = read_json(args.run_root / "relation_status.json")
    external_bridge = read_json(args.run_root / "external_bridge_status.json")
    status = {
        "schema": "polymarket_v6_runtime_status_v1",
        "timestamp": int(time.time()),
        "version": 6,
        "paper_only": True,
        "starting_capital": starting,
        "cash": f(maker.get("cash"), maker_start) + f(broker.get("cash"), broker_start) + f(external.get("cash"), external_start) + reserve,
        "equity": equity,
        "peak_equity": peak,
        "pnl": equity - starting,
        "drawdown": drawdown,
        "killed": killed,
        "live_units": maker_live + broker_live + external_live,
        "reserved_cash": reserved_cash,
        "gross_exposure": gross,
        "realized_pnl": f(external.get("realized_pnl")),
        "execution_imbalance": 0.0,
        "execution_staleness": 0.0,
        "strategies": {
            "micro_maker": {"equity": maker_eq, "pnl": maker_eq - maker_start, "live_units": maker_live, "killed": bool(int(f(maker.get("killed"))))},
            "local_factor_graph_structural": {"equity": broker_eq, "pnl": broker_eq - broker_start, "live_units": broker_live, "killed": bool(int(f(broker.get("killed"))))},
            "external": {"equity": external_eq, "pnl": external_eq - external_start, "live_units": external_live, "killed": bool(external.get("killed", False))},
        },
        "relations": relations,
        "external_bridge": external_bridge,
    }
    atomic_json(args.run_root / "runtime_status.json", status)
    print(json.dumps({k: status[k] for k in ("equity", "pnl", "drawdown", "live_units", "killed")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
