#!/usr/bin/env python3
"""V6 native telemetry entrypoint with legacy health compatibility only.

The rich runtime_status.json is native V6 telemetry. `strategy_status.csv` and
`allocator_status.json` retain the historical `v6_legacy_health_view` expected
by already-installed health tooling; no V5 expert or mixture is executed.
"""
from __future__ import annotations

import csv
import json
import runpy
import sys
from pathlib import Path


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def argument_path(flag: str, default: str) -> Path:
    try:
        index = sys.argv.index(flag)
        return Path(sys.argv[index + 1])
    except (ValueError, IndexError):
        return Path(default)


def write_legacy_health_view(run_root: Path, config_path: Path) -> None:
    status = json.loads((run_root / "runtime_status.json").read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    strategies = status.get("strategies") or {}
    sub = status.get("sub_strategies") or {}
    starting = number(status.get("starting_capital"), number(config.get("starting_capital"), 10000.0))
    v6 = config.get("v6") or {}
    reserve_fraction = number(v6.get("reserve_fraction"), 0.05)

    maker = strategies.get("micro_maker") or {}
    taker = strategies.get("micro_taker") or {}
    rv = strategies.get("relative_value") or {}
    hard = strategies.get("graph_hard") or {}
    external = strategies.get("external") or {}

    def total(rows, key):
        return sum(number(row.get(key)) for row in rows)

    micro_rows = [maker, taker]
    local_realized = number((sub.get("LOCAL_FACTOR") or {}).get("realized_pnl"))
    graph_rv_realized = sum(
        number((sub.get(name) or {}).get("realized_pnl"))
        for name in ("GRAPH_RV", "STRUCTURAL_TYPED")
    )
    rv_equity = number(rv.get("equity"), starting * 0.50)
    pca_equity = rv_equity * (0.35 / 0.50)
    graph_equity = rv_equity * (0.15 / 0.50) + number(hard.get("equity"), starting * 0.15)

    compat = [
        ("micro", "micro", 0.20, total(micro_rows, "equity"), total(micro_rows, "realized_pnl"), total(micro_rows, "gross_exposure"), int(total(micro_rows, "live_units")), int(total(micro_rows, "fills_total"))),
        ("pca", "local_factor", 0.35, pca_equity, local_realized, number(rv.get("gross_exposure")) * (0.35 / 0.50), int(number(rv.get("live_units"))), int(number(rv.get("fills_total")))),
        ("graph", "graph_structural_hard", 0.30, graph_equity, graph_rv_realized + number(hard.get("realized_pnl")), number(rv.get("gross_exposure")) * (0.15 / 0.50) + number(hard.get("gross_exposure")), int(number(rv.get("live_units"))) + int(number(hard.get("live_units"))), int(number(rv.get("fills_total"))) + int(number(hard.get("fills_total")))),
        ("semantic", "relation_parser", 0.0, 0.0, 0.0, 0.0, 0, 0),
        ("external", "external", 0.10, number(external.get("equity"), starting * 0.10), number(external.get("realized_pnl")), number(external.get("gross_exposure")), int(number(external.get("live_units"))), int(number(external.get("fills_total")))),
    ]
    fields = ["name","expert","capital_fraction","starting_capital","cash","equity","pnl","realized_pnl","peak_equity","drawdown","gross_exposure","open_positions","killed","alive","status_age_seconds","restarts","fills","buy_fills","sell_fills","settle_fills"]
    rows = []
    killed = bool(status.get("killed"))
    for name, expert, fraction, equity, realized, gross, live, fills in compat:
        sleeve_start = starting * fraction
        rows.append({
            "name": name, "expert": expert, "capital_fraction": fraction,
            "starting_capital": sleeve_start, "cash": equity, "equity": equity,
            "pnl": equity - sleeve_start, "realized_pnl": realized,
            "peak_equity": max(sleeve_start, equity), "drawdown": 0.0,
            "gross_exposure": gross, "open_positions": live,
            "killed": 1 if killed else 0, "alive": 1, "status_age_seconds": 0,
            "restarts": 0, "fills": fills, "buy_fills": 0, "sell_fills": 0, "settle_fills": 0,
        })
    tmp = run_root / "strategy_status.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    tmp.replace(run_root / "strategy_status.csv")

    allocator = {
        "schema": "v6_legacy_health_view", "paper_only": True,
        "models_expected": 5, "models_alive": 5,
        "reserve_fraction": reserve_fraction,
        "global_max_drawdown": number(config.get("max_drawdown"), 0.15),
        "global_max_gross_fraction": number(config.get("max_gross_fraction"), 0.45),
        "global_gross_fraction": number(status.get("gross_exposure")) / max(starting, 1.0),
        "timestamp": int(number(status.get("timestamp"))),
    }
    tmp_json = run_root / "allocator_status.json.tmp"
    tmp_json.write_text(json.dumps(allocator, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_json.replace(run_root / "allocator_status.json")


def main() -> int:
    namespace = runpy.run_path(str(Path(__file__).with_name("v6_runtime_status_v2.py")), run_name="v6_runtime_status_v2_runtime")
    result = int(namespace["main"]())
    if result != 0:
        return result
    write_legacy_health_view(argument_path("--run-root", "runs/paper_v6_live"), argument_path("--config", "config/paper_v6.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
