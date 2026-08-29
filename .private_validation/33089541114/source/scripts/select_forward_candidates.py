#!/usr/bin/env python3
"""Build an event-diverse forward-probe universe from reward opportunities."""
from __future__ import annotations

import argparse
import csv
import math
import os
import random
from pathlib import Path


def finite(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def locked_dollars(row: dict[str, str]) -> float:
    return max(0.0, finite(row.get("locked_complete_set_edge"))) * max(
        0.0, finite(row.get("quote_shares"))
    )


def balanced_score(row: dict[str, str]) -> float:
    activity = math.sqrt(1.0 + math.log1p(max(0.0, finite(row.get("volume24h")))))
    reward = max(0.0, finite(row.get("estimated_native_daily_value")))
    competition = max(0.0, finite(row.get("market_competitiveness")))
    return locked_dollars(row) * activity + 0.25 * reward + 0.01 / (1.0 + competition)


def select(rows: list[dict[str, str]], limit: int, seed: int) -> list[dict[str, str]]:
    valid = [
        row
        for row in rows
        if row.get("condition_id")
        and row.get("market_id")
        and finite(row.get("quote_shares")) > 0.0
        and finite(row.get("locked_complete_set_edge"), -1.0) >= 0.0
    ]
    rng = random.Random(seed)
    tie = {id(row): rng.random() for row in valid}
    rankings = [
        sorted(
            valid,
            key=lambda row: (
                finite(row.get("volume24h")),
                locked_dollars(row),
                tie[id(row)],
            ),
            reverse=True,
        ),
        sorted(
            valid,
            key=lambda row: (
                locked_dollars(row),
                finite(row.get("volume24h")),
                tie[id(row)],
            ),
            reverse=True,
        ),
        sorted(
            valid,
            key=lambda row: (
                balanced_score(row),
                finite(row.get("volume24h")),
                tie[id(row)],
            ),
            reverse=True,
        ),
    ]

    selected: list[dict[str, str]] = []
    selected_conditions: set[str] = set()
    seen_events: set[str] = set()
    cursors = [0] * len(rankings)

    def next_row(index: int, require_new_event: bool) -> dict[str, str] | None:
        ranking = rankings[index]
        while cursors[index] < len(ranking):
            row = ranking[cursors[index]]
            cursors[index] += 1
            condition = row.get("condition_id") or ""
            event = row.get("event_id") or condition
            if condition in selected_conditions:
                continue
            if require_new_event and event in seen_events:
                continue
            return row
        return None

    for require_new_event in (True, False):
        while len(selected) < max(0, limit):
            progressed = False
            for index in range(len(rankings)):
                row = next_row(index, require_new_event)
                if row is None:
                    continue
                selected.append(row)
                condition = row.get("condition_id") or ""
                selected_conditions.add(condition)
                seen_events.add(row.get("event_id") or condition)
                progressed = True
                if len(selected) >= limit:
                    break
            if not progressed:
                break
        if len(selected) >= limit:
            break
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markets", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args()
    if args.markets <= 0:
        raise SystemExit("--markets must be positive")

    with args.input.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("input has no CSV header")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    selected = select(rows, args.markets, args.seed)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, args.output)

    high_activity = sum(finite(row.get("volume24h")) >= 1000.0 for row in selected)
    print(
        "forward_candidate_selection"
        f" source_rows={len(rows)}"
        f" selected={len(selected)}"
        f" event_count={len({row.get('event_id') or row.get('condition_id') for row in selected})}"
        f" volume_ge_1000={high_activity}"
        f" max_volume={max((finite(row.get('volume24h')) for row in selected), default=0.0):.12g}"
        f" max_locked_dollars={max((locked_dollars(row) for row in selected), default=0.0):.12g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
