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


MISSING_AGE_SECONDS = 1e12
MODEL_FRESH_SECONDS = 120.0


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


def sum_csv(path: Path, column: str) -> float:
    return sum(f(row.get(column)) for row in read_csv(path))


def realized_fill_pnl(path: Path) -> float:
    """Reconstruct cumulative realized PnL without marking open inventory."""
    rows = read_csv(path)
    if any(str(row.get("pnl") or "").strip() for row in rows):
        return sum(f(row.get("pnl")) for row in rows)

    inventory: dict[tuple[str, str], list[float]] = {}
    realized = 0.0
    for row in rows:
        action = str(row.get("action") or "").upper()
        key = (str(row.get("market_id") or row.get("event_id") or ""), str(row.get("side") or ""))
        shares = max(0.0, f(row.get("shares")))
        price = max(0.0, f(row.get("price")))
        fee = max(0.0, f(row.get("fee")))
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


def max_fill_imbalance(path: Path, bundles_path: Path) -> float:
    live_bundle_ids = {
        str(row.get("bundle_id") or "")
        for row in read_csv(bundles_path)
        if str(row.get("status") or "").upper() in {"RESTING", "COMPLETE", "ABORTING"}
    }
    by_bundle: dict[str, list[float]] = {}
    for row in read_csv(path):
        bundle_id = str(row.get("bundle_id") or "")
        if bundle_id not in live_bundle_ids:
            continue
        target = max(0.0, f(row.get("target_shares")))
        filled = max(0.0, f(row.get("filled_shares")))
        fraction = min(1.0, max(0.0, filled / target)) if target > 1e-12 else 0.0
        by_bundle.setdefault(bundle_id, []).append(fraction)
    return max((max(values) - min(values) for values in by_bundle.values() if values), default=0.0)


def timestamp_age(row: dict, now: float, *timestamp_keys: str) -> float:
    """Age a model output only when it contains a valid producer timestamp."""
    if not row or not timestamp_keys:
        return MISSING_AGE_SECONDS
    for key in timestamp_keys:
        timestamp = f(row.get(key), -1.0)
        if timestamp > 1e12:
            timestamp /= 1000.0
        if timestamp > 0.0:
            if timestamp > now + 30.0:
                return MISSING_AGE_SECONDS
            return max(0.0, now - timestamp)
    return MISSING_AGE_SECONDS


def file_mtime_age(path: Path, now: float) -> float:
    """Use mtime only for a validated heartbeat file without a timestamp field."""
    try:
        timestamp = path.stat().st_mtime
    except OSError:
        return MISSING_AGE_SECONDS
    if timestamp > now + 30.0:
        return MISSING_AGE_SECONDS
    return max(0.0, now - timestamp)


def fresh(age: float) -> int:
    return int(math.isfinite(age) and age <= MODEL_FRESH_SECONDS)


def temporary_path(path: Path) -> Path:
    return path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temporary_path(path)
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temporary_path(path)
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

    now = time.time()
    maker_path = args.run_root / "maker" / "maker_equity.csv"
    micro_path = args.run_root / "micro_taker" / "status.json"
    broker_path = args.run_root / "multileg_equity.csv"
    hard_path = args.run_root / "hard_arb" / "status.json"
    external_path = args.run_root / "external" / "status.json"
    maker = last_csv(maker_path)
    micro = read_json(micro_path)
    broker = last_csv(broker_path)
    hard = read_json(hard_path)
    external = read_json(external_path)

    maker_fills = fill_counts(args.run_root / "maker" / "maker_fills.csv")
    micro_fills = fill_counts(args.run_root / "micro_taker" / "fills.csv")
    hard_fills = fill_counts(args.run_root / "hard_arb" / "fills.csv")
    external_fills = fill_counts(args.run_root / "external" / "fills.csv")

    maker_realized = realized_fill_pnl(args.run_root / "maker" / "maker_fills.csv")
    micro_realized = f(micro.get("realized_pnl"))
    broker_realized = sum_csv(args.run_root / "bundle_ledger.csv", "net_pnl")
    hard_realized = f(hard.get("realized_pnl"))
    external_realized = realized_fill_pnl(args.run_root / "external" / "fills.csv")

    maker_age = timestamp_age(maker, now, "timestamp")
    micro_age = timestamp_age(micro, now, "timestamp")
    broker_age = timestamp_age(broker, now, "timestamp")
    hard_age = timestamp_age(hard, now, "timestamp")
    external_age = timestamp_age(external, now, "timestamp")
    recorder_state_path = args.run_root / "trade_recorder_state.csv"
    recorder_state = last_csv(recorder_state_path)
    recorder_age = file_mtime_age(recorder_state_path, now) if recorder_state else MISSING_AGE_SECONDS
    recorder_trade_age = timestamp_age(recorder_state, now, "last_trade_ts")
    supervisor_path = args.run_root / "runtime_supervisor.csv"
    supervisor = last_csv(supervisor_path)
    supervisor_ready = bool(supervisor) and all(
        str(supervisor.get(field) or "") == "1"
        for field in ("recorder_alive", "broker_alive", "allocator_alive")
    )
    supervisor_age = timestamp_age(supervisor, now, "timestamp") if supervisor_ready else MISSING_AGE_SECONDS
    proxy_path = args.run_root / "market_proxy_status.json"
    proxy = read_json(proxy_path)
    proxy_ready = (
        bool(proxy)
        and str(proxy.get("source") or "") not in {"", "startup", "unavailable"}
        and f(proxy.get("markets"), -1.0) > 0.0
        and 0.0 <= f(proxy.get("cache_age_seconds"), MISSING_AGE_SECONDS) <= 900.0
    )
    proxy_age = timestamp_age(proxy, now, "timestamp") if proxy_ready else MISSING_AGE_SECONDS

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

    relations_path = args.run_root / "relation_status.json"
    graph_research_path = args.run_root / "graph_research_status.json"
    local_factor_path = args.run_root / "local_factor_status.json"
    bridge_path = args.run_root / "external_bridge_status.json"
    relations = read_json(relations_path)
    graph_research = read_json(graph_research_path)
    local_factor = read_json(local_factor_path)
    bridge = read_json(bridge_path)
    relation_age = timestamp_age(relations, now, "timestamp")
    graph_research_age = timestamp_age(graph_research, now, "timestamp")
    local_factor_age = timestamp_age(local_factor, now, "timestamp")
    bridge_age = timestamp_age(bridge, now, "timestamp")

    strategies = {
        "micro_maker": {
            "equity": maker_eq,
            "pnl": maker_eq - alloc["micro_maker"],
            "live_units": maker_live,
            "killed": bool(int(f(maker.get("killed")))),
            "realized_pnl": maker_realized,
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
            "exploration": micro.get("exploration") if isinstance(micro.get("exploration"), dict) else {},
            "realized_pnl": micro_realized,
            **micro_fills,
        },
        "relative_value": {
            "equity": broker_eq,
            "pnl": broker_eq - alloc["relative_value"],
            "live_units": broker_live,
            "killed": bool(int(f(broker.get("killed")))),
            "realized_pnl": broker_realized,
        },
        "graph_hard": {
            "equity": hard_eq,
            "pnl": hard_eq - alloc["hard_arb"],
            "live_units": hard_live,
            "killed": bool(hard.get("killed", False)),
            "signals": int(f(hard.get("positive_candidates"))),
            "best_edge": f(hard.get("best_edge")),
            "entered": int(f(hard.get("entered"))),
            "realized_pnl": hard_realized,
            **hard_fills,
        },
        "external": {
            "equity": external_eq,
            "pnl": external_eq - alloc["external"],
            "live_units": external_live,
            "killed": bool(external.get("killed", False)),
            "realized_pnl": external_realized,
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
    component_ages = {
        "maker": maker_age,
        "micro_taker": micro_age,
        "multileg_broker": broker_age,
        "hard_arb": hard_age,
        "external": external_age,
        "trade_recorder": recorder_age,
        "supervisor": supervisor_age,
        "market_proxy": proxy_age,
        "relations": relation_age,
        "graph_research": graph_research_age,
        "local_factor": local_factor_age,
        "external_bridge": bridge_age,
    }
    unready_components = sorted(
        name for name, age in component_ages.items()
        if not math.isfinite(age) or age > MODEL_FRESH_SECONDS
    )
    status = {
        "schema": "polymarket_v6_runtime_status_v1",
        "timestamp": int(now),
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
        "realized_pnl": maker_realized + micro_realized + broker_realized + hard_realized + external_realized,
        "execution_imbalance": max_fill_imbalance(
            args.run_root / "multileg_legs.csv", args.run_root / "multileg_bundles.csv"
        ),
        "execution_staleness": max(component_ages.values()),
        "component_staleness_seconds": component_ages,
        "unready_components": unready_components,
        "proxy_ready": proxy_ready,
        "supervisor_ready": supervisor_ready,
        "trade_recorder_last_trade_age_seconds": recorder_trade_age,
        "strategies": strategies,
        "relations": relations,
        "graph_research": graph_research,
        "local_factor": local_factor,
        "external_bridge": bridge,
    }
    atomic_json(args.run_root / "runtime_status.json", status)

    # Transitional V5-shaped telemetry only: no V5 expert or mixture is restored.
    # Fill counts are sourced from actual V6 ledgers rather than being hard-coded
    # to zero. Relative-value events stay uncounted until that sleeve exposes a
    # cumulative fill ledger of its own; current live legs are not fills.
    micro_counts = {k: maker_fills[k] + micro_fills[k] for k in maker_fills}
    graph_counts = hard_fills
    zero_counts = {"fills": 0, "buy_fills": 0, "sell_fills": 0, "settle_fills": 0}
    compat = [
        ("micro", "micro", .20, maker_eq + micro_eq, maker_live + micro_live, micro_counts, max(maker_age, micro_age)),
        ("pca", "local_factor", .50, broker_eq, broker_live, zero_counts, max(local_factor_age, broker_age)),
        ("graph", "graph_research_plus_structural_hard", .15, hard_eq, hard_live, graph_counts, max(graph_research_age, hard_age)),
        ("semantic", "relation_parser", 0.0, 0.0, 0, zero_counts, relation_age),
        ("external", "external", .10, external_eq, external_live, external_fills, max(bridge_age, external_age)),
    ]
    fields = [
        "name", "expert", "capital_fraction", "starting_capital", "cash", "equity", "pnl",
        "realized_pnl", "peak_equity", "drawdown", "gross_exposure", "open_positions", "killed",
        "alive", "status_age_seconds", "restarts", "fills", "buy_fills", "sell_fills", "settle_fills",
    ]
    rows = []
    for name, expert, frac, eq, live, counts, status_age in compat:
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
            "alive": fresh(status_age),
            "status_age_seconds": status_age,
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
            "models_alive": sum(fresh(row[-1]) for row in compat),
            "reserve_fraction": reserve_frac,
            "global_max_drawdown": f(cfg.get("max_drawdown"), .15),
            "global_max_gross_fraction": f(cfg.get("max_gross_fraction"), .45),
            "global_gross_fraction": gross / max(starting, 1.0),
            "timestamp": int(now),
        },
    )
    print(json.dumps({k: status[k] for k in ("equity", "pnl", "drawdown", "live_units", "killed")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
