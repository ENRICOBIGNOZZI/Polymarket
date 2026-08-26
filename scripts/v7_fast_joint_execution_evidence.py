#!/usr/bin/env python3
"""Aggregate raw V7 Fast Arb PAPER joint-execution outcomes.

Input rows must describe prospective PAPER execution outcomes produced by the fast
server event loop.  This script never infers fills from quoted opportunities and
never accepts mixed-SHA evidence.  It only aggregates observed joint states, realized
liquidation PnL, explicit costs, partial/unwind outcomes and capital duration into the
contract consumed by ``v7_fast_structural_gate.py``.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_fast_joint_execution_v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def integer(value: Any, default: int = 0) -> int:
    result = finite(value)
    return int(result) if math.isfinite(result) else default


def boolean(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        return [dict(row) for row in csv.DictReader(handle) if row]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def aggregate(rows: list[dict[str, str]], *, expected_sha: str) -> dict[str, Any]:
    if not SHA_RE.fullmatch(expected_sha):
        raise ValueError("expected_sha must be an exact 40-character lowercase Git SHA")
    if any((row.get("model_sha") or "").strip() != expected_sha for row in rows):
        raise ValueError("mixed_or_wrong_sha_execution_rows")

    joint_counts: Counter[str] = Counter()
    filled_rows: list[dict[str, str]] = []
    explicit_cost_sum = 0.0
    net_pnl_sum = 0.0
    capital_seconds = 0.0
    completed_baskets = 0
    partial_rows = 0
    partial_unwind_ok = True
    point_in_time = bool(rows)
    authoritative_fees = bool(rows)
    depth_executable = bool(rows)

    for row in rows:
        state = (row.get("joint_state") or "UNKNOWN").strip().upper() or "UNKNOWN"
        joint_counts[state] += 1
        target_legs = max(0, integer(row.get("target_legs")))
        filled_legs = max(0, integer(row.get("filled_legs")))
        pnl = finite(row.get("net_pnl"))
        cost = finite(row.get("explicit_cost"))
        duration = finite(row.get("capital_seconds"), 0.0)
        if not math.isfinite(pnl):
            continue
        if not math.isfinite(cost) or cost < 0.0:
            raise ValueError("execution row has invalid explicit_cost")
        if duration < 0.0 or not math.isfinite(duration):
            raise ValueError("execution row has invalid capital_seconds")

        point_in_time = point_in_time and boolean(row.get("point_in_time"))
        authoritative_fees = authoritative_fees and boolean(row.get("authoritative_fees"))
        depth_executable = depth_executable and boolean(row.get("depth_executable"))
        is_partial = boolean(row.get("partial_unwind")) or (0 < filled_legs < target_legs)
        if is_partial:
            partial_rows += 1
            partial_unwind_ok = partial_unwind_ok and boolean(row.get("unwind_accounted"))
        if filled_legs > 0:
            filled_rows.append(row)
            net_pnl_sum += pnl
            explicit_cost_sum += cost
            capital_seconds += duration
        if (
            boolean(row.get("completed_basket"))
            and target_legs > 0
            and filled_legs == target_legs
        ):
            completed_baskets += 1

    if not rows:
        partial_unwind_ok = False

    return {
        "schema": SCHEMA,
        "model_sha": expected_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "point_in_time": point_in_time,
        "authoritative_fees": authoritative_fees,
        "depth_executable": depth_executable,
        "partial_unwind_accounted": partial_unwind_ok,
        "joint_state_observations": len(rows),
        "realized_pnl_observations": len(filled_rows),
        "completed_baskets": completed_baskets,
        "partial_unwind_observations": partial_rows,
        "joint_state_counts": dict(sorted(joint_counts.items())),
        "fill_conditioned_net_pnl": net_pnl_sum,
        "explicit_cost_sum": explicit_cost_sum,
        "cost_stress_1_5x_net_pnl": net_pnl_sum - 0.5 * explicit_cost_sum,
        "cost_stress_2x_net_pnl": net_pnl_sum - explicit_cost_sum,
        "capital_hours": capital_seconds / 3600.0,
        "pnl_per_capital_hour": (
            net_pnl_sum / (capital_seconds / 3600.0)
            if capital_seconds > 0.0
            else None
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = read_rows(args.input)
    report = aggregate(rows, expected_sha=args.expected_sha)
    atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
