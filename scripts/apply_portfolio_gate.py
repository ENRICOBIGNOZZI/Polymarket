#!/usr/bin/env python3
"""Read or apply the portfolio-champion new-exposure gate.

This script never changes live positions or broker state. Filtering mode admits
or suppresses *new* complete intent bundles before the existing broker reads
them. Check-only mode is used by other alpha sleeves before creating new paper
orders. Missing, stale, malformed or globally killed supervisor state fails
closed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode",
    "expected_edge", "max_notional", "market_id", "side", "weight",
    "limit_price", "execution_deadline_ts", "hold_deadline_ts",
]


def read_gate(path: Path, engine: str, now: int, max_age: int) -> tuple[bool, float, str]:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, 0.0, f"gate_unavailable:{type(exc).__name__}"
    if not isinstance(root, dict) or root.get("schema_version") != 1:
        return False, 0.0, "gate_schema_invalid"
    try:
        timestamp = int(root.get("timestamp", 0))
    except (TypeError, ValueError):
        return False, 0.0, "gate_timestamp_invalid"
    if timestamp <= 0 or timestamp > now + 5 or now - timestamp > max_age:
        return False, 0.0, "gate_stale"
    if bool(root.get("global_kill", True)):
        return False, 0.0, "global_kill"
    engines = root.get("engines")
    if not isinstance(engines, dict) or not isinstance(engines.get(engine), dict):
        return False, 0.0, "engine_gate_missing"
    gate = engines[engine]
    if not bool(gate.get("new_exposure_allowed", False)):
        return False, 0.0, str(gate.get("reason") or "new_exposure_closed")
    try:
        capital = float(gate.get("capital_limit_usd", 0.0))
    except (TypeError, ValueError):
        return False, 0.0, "capital_limit_invalid"
    if not math.isfinite(capital) or capital <= 0.0:
        return False, 0.0, "capital_limit_closed"
    return True, capital, "ok"


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or any(field not in reader.fieldnames for field in FIELDS):
            raise ValueError("incompatible intent schema")
        return [{field: (row.get(field) or "").strip() for field in FIELDS} for row in reader]


def atomic_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def apply_gate(rows: list[dict[str, str]], allowed: bool, capital_limit: float) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["bundle_id"]].append(row)
    if not allowed:
        return [], len(grouped)

    bundles: list[tuple[float, str, list[dict[str, str]]]] = []
    rejected = 0
    for bundle_id, bundle_rows in grouped.items():
        if not bundle_id or not bundle_rows:
            rejected += 1
            continue
        try:
            cap = float(bundle_rows[0]["max_notional"])
            edge = float(bundle_rows[0]["expected_edge"])
        except (TypeError, ValueError):
            rejected += 1
            continue
        if not math.isfinite(cap) or cap <= 0.0 or not math.isfinite(edge):
            rejected += 1
            continue
        bundles.append((edge, bundle_id, bundle_rows))

    bundles.sort(key=lambda item: (item[0], item[1]), reverse=True)
    remaining = capital_limit
    admitted: list[dict[str, Any]] = []
    for _edge, _bundle_id, bundle_rows in bundles:
        requested = float(bundle_rows[0]["max_notional"])
        allocation = min(requested, remaining)
        if allocation <= 1e-9:
            rejected += 1
            continue
        for row in bundle_rows:
            updated: dict[str, Any] = dict(row)
            updated["max_notional"] = f"{allocation:.12g}"
            admitted.append(updated)
        remaining -= allocation
    return admitted, rejected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--gate", type=Path, default=Path("runs/supervisor/capital_limits.json"))
    parser.add_argument("--engine", default="alpha")
    parser.add_argument("--max-age-seconds", type=int, default=30)
    parser.add_argument("--now", type=int, default=None)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    now = int(time.time()) if args.now is None else args.now
    allowed, capital_limit, reason = read_gate(
        args.gate, args.engine, now, max(1, args.max_age_seconds)
    )
    summary: dict[str, Any] = {
        "engine": args.engine,
        "allowed": allowed,
        "reason": reason,
        "capital_limit_usd": capital_limit,
    }
    if args.check_only:
        print(json.dumps(summary, sort_keys=True))
        return 0 if allowed else 1

    if args.input is None or args.output is None:
        parser.error("--input and --output are required unless --check-only is used")
    rows = load_rows(args.input)
    admitted, rejected = apply_gate(rows, allowed, capital_limit)
    atomic_write(args.output, admitted)
    summary.update(
        {
            "input_rows": len(rows),
            "output_rows": len(admitted),
            "rejected_bundles": rejected,
        }
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
