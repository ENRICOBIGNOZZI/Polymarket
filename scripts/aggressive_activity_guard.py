#!/usr/bin/env python3
"""Verify that the V5 runtime is hunting a broad market universe, not merely alive."""
from __future__ import annotations

import argparse
import csv
import json
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
    return result if result == result and abs(result) != float("inf") else default


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

    for row in rows:
        name = row.get("name", "unknown")
        alive = as_int(row.get("alive")) == 1
        status_age = as_float(row.get("status_age_seconds"), float("inf"))
        engine_log = args.run_root / "strategies" / name / "engine.log"
        engine_age = now - engine_log.stat().st_mtime if engine_log.exists() else float("inf")
        status = latest_engine_status(engine_log)

        if alive:
            alive_models += 1
        elif not args.allow_stopped:
            failures.append(f"{name}:not_alive")
        if not args.allow_stopped and status_age > args.max_model_staleness_seconds:
            failures.append(f"{name}:status_stale:{status_age:.1f}")
        if engine_age > args.max_model_staleness_seconds:
            failures.append(f"{name}:engine_log_stale:{engine_age:.1f}")
        else:
            fresh_models += 1

        discovered = int(status["discovered"]) if status else 0
        tradable = int(status["tradable"]) if status else 0
        candidates = int(status["candidates"]) if status else 0
        fraction = tradable / discovered if discovered > 0 else 0.0
        total_discovered += discovered
        total_tradable += tradable
        total_candidates += candidates

        if status is None:
            failures.append(f"{name}:missing_engine_status")
        else:
            if discovered < args.minimum_markets:
                failures.append(f"{name}:narrow_universe:{discovered}")
            if fraction < args.minimum_tradable_fraction:
                failures.append(f"{name}:tradable_fraction:{fraction:.4f}")

        models.append(
            {
                "name": name,
                "alive": alive,
                "status_age_seconds": status_age,
                "engine_log_age_seconds": engine_age,
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
        "models": models,
    }
    atomic_json(args.output, activity)
    print(json.dumps(activity, sort_keys=True))
    return 0 if activity["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
