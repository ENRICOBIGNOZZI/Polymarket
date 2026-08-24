#!/usr/bin/env python3
"""Verify that the V5 runtime is hunting a broad market universe, not merely alive."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

STATUS_RE = re.compile(
    r"discovered=(?P<discovered>\d+)\s+"
    r"tradable=(?P<tradable>\d+)\s+"
    r"candidates=(?P<candidates>\d+)"
)
EXPECTED_NAMES = {"micro", "pca", "graph", "semantic", "external"}


def as_float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def file_age(path: Path, now: float) -> float:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return float("inf")


def latest_engine_status(path: Path) -> dict[str, int] | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        match = STATUS_RE.search(line)
        if match:
            return {key: int(value) for key, value in match.groupdict().items()}
    return None


def recent_market_count(path: Path, cutoff: int) -> int:
    markets: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if as_int(row.get("timestamp")) < cutoff:
                    continue
                market = str(row.get("market_id") or "")
                if market:
                    markets.add(market)
    except (OSError, csv.Error):
        return 0
    return len(markets)


def recent_row_count(path: Path, cutoff: int) -> int:
    count = 0
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if as_int(row.get("timestamp")) >= cutoff:
                    count += 1
    except (OSError, csv.Error):
        return 0
    return count


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--expected-models", type=int, default=5)
    parser.add_argument("--minimum-markets", type=int, default=100)
    parser.add_argument("--minimum-tradable-fraction", type=float, default=0.10)
    parser.add_argument("--max-model-staleness-seconds", type=float, default=180.0)
    parser.add_argument("--allow-stopped", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args()

    now = time.time() if args.now is None else float(args.now)
    rows = read_rows(args.run_root / "strategy_status.csv")
    failures: list[str] = []
    models: list[dict[str, Any]] = []

    names = {row.get("name", "") for row in rows if row.get("name")}
    if len(rows) != args.expected_models:
        failures.append(f"models_expected:{args.expected_models}:observed:{len(rows)}")
    if args.expected_models == 5 and names != EXPECTED_NAMES:
        failures.append("model_names_mismatch")

    total_discovered = 0
    total_tradable = 0
    total_candidates = 0
    fresh_models = 0
    alive_models = 0
    cutoff = int(now - args.max_model_staleness_seconds)

    for row in rows:
        name = row.get("name", "unknown")
        alive = as_int(row.get("alive")) == 1
        manager_status_age = as_float(row.get("status_age_seconds"), float("inf"))
        model_root = args.run_root / "strategies" / name
        engine_log = model_root / "engine.log"
        status_path = model_root / "status.json"
        history_path = model_root / "history.csv"
        signals_path = model_root / "signals.csv"
        config_path = args.run_root / "generated_configs" / f"{name}.json"

        engine_age = file_age(engine_log, now)
        state_age = file_age(status_path, now)
        history_age = file_age(history_path, now)
        signals_age = file_age(signals_path, now)
        persisted_activity_age = min(state_age, history_age, signals_age)
        status = latest_engine_status(engine_log)
        config = read_json(config_path)

        if alive:
            alive_models += 1
        elif not args.allow_stopped:
            failures.append(f"{name}:not_alive")
        if not args.allow_stopped and manager_status_age > args.max_model_staleness_seconds:
            failures.append(f"{name}:manager_status_stale:{manager_status_age:.1f}")
        if persisted_activity_age > args.max_model_staleness_seconds and engine_age > args.max_model_staleness_seconds:
            failures.append(
                f"{name}:activity_stale:{min(persisted_activity_age, engine_age):.1f}"
            )
        else:
            fresh_models += 1

        # C++ stdout may be block-buffered when redirected to a regular file. Use
        # the exact status line when available, otherwise reconstruct the funnel
        # from files that are closed/flushed every scan: generated config,
        # history.csv and signals.csv.
        discovered = int(status["discovered"]) if status else as_int(config.get("market_limit"))
        tradable = int(status["tradable"]) if status and engine_age <= args.max_model_staleness_seconds else recent_market_count(history_path, cutoff)
        candidates = int(status["candidates"]) if status and engine_age <= args.max_model_staleness_seconds else recent_row_count(signals_path, cutoff)
        fraction = tradable / discovered if discovered > 0 else 0.0
        total_discovered += discovered
        total_tradable += tradable
        total_candidates += candidates

        if discovered < args.minimum_markets:
            failures.append(f"{name}:narrow_universe:{discovered}")
        if fraction < args.minimum_tradable_fraction:
            failures.append(f"{name}:tradable_fraction:{fraction:.4f}")

        models.append(
            {
                "name": name,
                "alive": alive,
                "manager_status_age_seconds": manager_status_age,
                "engine_log_age_seconds": engine_age,
                "persisted_activity_age_seconds": persisted_activity_age,
                "funnel_source": "engine_log" if status and engine_age <= args.max_model_staleness_seconds else "persisted_files",
                "discovered": discovered,
                "tradable": tradable,
                "tradable_fraction": fraction,
                "candidates": candidates,
                "fills": as_int(row.get("fills")),
                "open_positions": as_int(row.get("open_positions")),
                "pnl": as_float(row.get("pnl")),
            }
        )

    if rows and total_candidates <= 0:
        failures.append("zero_candidates_across_all_models")

    maker_orders = read_rows(args.run_root / "maker" / "maker_order_log.csv")
    maker_fills = read_rows(args.run_root / "maker" / "maker_fills.csv")
    intent_rows = read_rows(args.run_root / "intents.csv")
    external_rows = read_rows(Path("data/external_signals.csv"))
    activity = {
        "schema": "polymarket_aggressive_activity_v1",
        "timestamp": int(now),
        "healthy": not failures,
        "failures": failures,
        "sla": {
            "expected_models": args.expected_models,
            "minimum_markets_per_model": args.minimum_markets,
            "minimum_tradable_fraction": args.minimum_tradable_fraction,
            "max_model_staleness_seconds": args.max_model_staleness_seconds,
            "allow_stopped": args.allow_stopped,
        },
        "models_alive": alive_models,
        "models_fresh": fresh_models,
        "total_discovered": total_discovered,
        "total_tradable": total_tradable,
        "total_candidates": total_candidates,
        "maker_orders": len(maker_orders),
        "maker_fills": len(maker_fills),
        "multileg_intent_legs": len(intent_rows),
        "external_signal_rows": len(external_rows),
        "models": models,
    }
    atomic_json(args.output, activity)
    print(json.dumps(activity, sort_keys=True))
    return 0 if activity["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
