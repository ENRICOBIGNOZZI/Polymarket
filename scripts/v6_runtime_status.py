#!/usr/bin/env python3
"""V6 native telemetry entrypoint with legacy health compatibility only.

The rich runtime_status.json is native V6 telemetry. `strategy_status.csv` and
`allocator_status.json` retain the historical `v6_legacy_health_view` expected
by already-installed health tooling; no V5 expert or mixture is executed.
"""
from __future__ import annotations

import csv
import json
import math
import runpy
import sys
import time
from collections import Counter
from pathlib import Path


def number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def argument_path(flag: str, default: str) -> Path:
    try:
        index = sys.argv.index(flag)
        return Path(sys.argv[index + 1])
    except (ValueError, IndexError):
        return Path(default)


def csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def last_csv(path: Path) -> dict[str, str]:
    rows = csv_rows(path)
    return rows[-1] if rows else {}


def file_age(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 1e9


def maker_realized_pnl(rows: list[dict[str, str]]) -> float:
    if any(str(row.get("pnl") or "").strip() for row in rows):
        return sum(number(row.get("pnl")) for row in rows)
    inventory: dict[tuple[str, str], list[float]] = {}
    realized = 0.0
    for row in rows:
        action = str(row.get("action") or "").upper()
        key = (str(row.get("market_id") or ""), str(row.get("side") or ""))
        shares = max(0.0, number(row.get("shares")))
        price = max(0.0, number(row.get("price")))
        fee = max(0.0, number(row.get("fee")))
        if shares <= 0.0:
            continue
        if action.startswith("BUY"):
            state = inventory.setdefault(key, [0.0, 0.0])
            state[0] += shares
            state[1] += shares * price + fee
        elif action.startswith("SELL") or "SETTLE" in action:
            state = inventory.setdefault(key, [0.0, 0.0])
            if state[0] <= 1e-12:
                continue
            closed = min(shares, state[0])
            average_cost = state[1] / state[0]
            fee_share = fee * (closed / shares)
            realized += closed * price - fee_share - closed * average_cost
            state[0] -= closed
            state[1] = max(0.0, state[1] - closed * average_cost)
    return realized


def enrich_native_status(run_root: Path, config_path: Path) -> None:
    status_path = run_root / "runtime_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    strategies = status.get("strategies") or {}
    maker = strategies.get("micro_maker")
    if not isinstance(maker, dict):
        return

    maker_equity_path = run_root / "maker" / "maker_equity.csv"
    maker_equity = last_csv(maker_equity_path)
    maker_orders_rows = csv_rows(run_root / "maker" / "maker_orders.csv")
    maker_positions_rows = csv_rows(run_root / "maker" / "maker_positions.csv")
    maker_fill_rows = csv_rows(run_root / "maker" / "maker_fills.csv")
    maker_order_log = csv_rows(run_root / "maker" / "maker_order_log.csv")
    maker_fraction = number((config.get("v6") or {}).get("micro_maker_capital_fraction"), 0.12)
    maker_start = number(config.get("starting_capital"), 10000.0) * maker_fraction

    if maker_equity:
        old_equity = number(maker.get("equity"), maker_start)
        old_reserved = number(status.get("reserved_cash"))
        old_cash = number(status.get("cash"))
        actual_equity = number(maker_equity.get("equity"), maker_start)
        actual_cash = number(maker_equity.get("cash"), maker_start)
        reserved = number(maker_equity.get("reserved_cash"))
        position_cost = sum(
            max(0.0, number(row.get("shares"))) * max(0.0, number(row.get("entry_price"), number(row.get("cost"))))
            for row in maker_positions_rows
        )
        actions = Counter(str(row.get("action") or "").upper() for row in maker_order_log)
        realized = maker_realized_pnl(maker_fill_rows)
        best_edge = max((number(row.get("signal_edge"), float("-inf")) for row in maker_order_log), default=0.0)
        if not math.isfinite(best_edge):
            best_edge = 0.0

        maker.update({
            "equity": actual_equity,
            "pnl": actual_equity - maker_start,
            "realized_pnl": realized,
            "gross_exposure": reserved + position_cost,
            "drawdown": number(maker_equity.get("drawdown")),
            "live_units": int(number(maker_equity.get("resting_orders"))) + int(number(maker_equity.get("positions"))),
            "orders_total": actions.get("POST", 0),
            "fills_total": len(maker_fill_rows),
            "status_age_seconds": file_age(maker_equity_path),
            "killed": bool(int(number(maker_equity.get("killed")))),
            "signals": int(number(maker_equity.get("signals"), number(maker.get("signals")))),
            "best_edge": number(maker_equity.get("best_edge"), best_edge),
        })
        status["cash"] = old_cash + actual_cash - maker_start
        status["equity"] = number(status.get("equity")) + actual_equity - old_equity
        status["pnl"] = status["equity"] - number(status.get("starting_capital"), number(config.get("starting_capital"), 10000.0))
        status["reserved_cash"] = old_reserved + reserved

    existing_orders = [row for row in (status.get("open_orders") or []) if isinstance(row, dict) and row.get("model") != "micro_maker"]
    for row in maker_orders_rows:
        existing_orders.append({
            "model": "micro_maker", "strategy": "MICRO_MAKER", "bundle_id": "",
            "market_id": row.get("market_id") or "", "side": row.get("side") or "",
            "state": "RESTING", "limit_price": number(row.get("limit_price")),
            "remaining_shares": number(row.get("remaining_shares"), number(row.get("shares"))),
            "queue_ahead": number(row.get("queue_ahead")),
        })
    status["open_orders"] = existing_orders[:100]
    status["open_order_count"] = len(status["open_orders"])

    guard = status.get("relation_guard")
    if isinstance(guard, dict) and "accepted_bundles" not in guard:
        relation_rows = csv_rows(run_root / "relation_intents.csv")
        guard["accepted_bundles"] = len({str(row.get("bundle_id") or "") for row in relation_rows if row.get("bundle_id")})

    status["realized_pnl"] = sum(number(row.get("realized_pnl")) for row in strategies.values() if isinstance(row, dict))
    status["gross_exposure"] = sum(number(row.get("gross_exposure")) for row in strategies.values() if isinstance(row, dict))
    status["live_units"] = sum(int(number(row.get("live_units"))) for row in strategies.values() if isinstance(row, dict))
    status["fill_count_total"] = sum(int(number(row.get("fills_total"))) for row in strategies.values() if isinstance(row, dict))
    status["peak_equity"] = max(number(status.get("peak_equity")), number(status.get("equity")))
    peak = number(status.get("peak_equity"))
    status["drawdown"] = max(0.0, 1.0 - number(status.get("equity")) / peak) if peak > 0.0 else 0.0
    tmp = status_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(status_path)


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
    graph_rv_realized = sum(number((sub.get(name) or {}).get("realized_pnl")) for name in ("GRAPH_RV", "STRUCTURAL_TYPED"))
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
        rows.append({"name":name,"expert":expert,"capital_fraction":fraction,"starting_capital":sleeve_start,"cash":equity,"equity":equity,"pnl":equity-sleeve_start,"realized_pnl":realized,"peak_equity":max(sleeve_start,equity),"drawdown":0.0,"gross_exposure":gross,"open_positions":live,"killed":1 if killed else 0,"alive":1,"status_age_seconds":0,"restarts":0,"fills":fills,"buy_fills":0,"sell_fills":0,"settle_fills":0})
    tmp = run_root / "strategy_status.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    tmp.replace(run_root / "strategy_status.csv")

    allocator = {"schema":"v6_legacy_health_view","paper_only":True,"models_expected":5,"models_alive":5,"reserve_fraction":reserve_fraction,"global_max_drawdown":number(config.get("max_drawdown"),0.15),"global_max_gross_fraction":number(config.get("max_gross_fraction"),0.45),"global_gross_fraction":number(status.get("gross_exposure"))/max(starting,1.0),"timestamp":int(number(status.get("timestamp")))}
    tmp_json = run_root / "allocator_status.json.tmp"
    tmp_json.write_text(json.dumps(allocator, indent=2, sort_keys=True) + "\n", encoding="utf-8"); tmp_json.replace(run_root / "allocator_status.json")


def main() -> int:
    run_root = argument_path("--run-root", "runs/paper_v6_live")
    config_path = argument_path("--config", "config/paper_v6.json")
    namespace = runpy.run_path(str(Path(__file__).with_name("v6_runtime_status_v2.py")), run_name="v6_runtime_status_v2_runtime")
    result = int(namespace["main"]())
    if result != 0:
        return result
    enrich_native_status(run_root, config_path)
    write_legacy_health_view(run_root, config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
