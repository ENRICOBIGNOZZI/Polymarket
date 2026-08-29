#!/usr/bin/env python3
"""Atomically merge complete V7 bundle intents for the single multileg broker."""
from __future__ import annotations

import argparse
import csv
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode",
    "expected_edge", "max_notional", "market_id", "side", "weight",
    "limit_price", "execution_deadline_ts", "hold_deadline_ts",
]


def load_file(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or any(field not in reader.fieldnames for field in FIELDS):
            raise ValueError(f"{path}: incompatible intent schema")
        return [{key: (row.get(key) or "").strip() for key in FIELDS} for row in reader]


def valid_bundle(rows: list[dict[str, str]], now: int, min_edge: float, max_age: int) -> bool:
    if len(rows) < 2:
        return False
    head = rows[0]
    if any(row["bundle_id"] != head["bundle_id"] or row["strategy"] != head["strategy"] for row in rows):
        return False
    if len({(row["market_id"], row["side"]) for row in rows}) != len(rows):
        return False
    try:
        created = int(head["created_ts"])
        deadline = int(head["execution_deadline_ts"])
        hold = int(head["hold_deadline_ts"])
        edge = float(head["expected_edge"])
        notional = float(head["max_notional"])
        if created > now + 5 or now - created > max_age or deadline <= now or hold <= deadline:
            return False
        if edge < min_edge or notional <= 0 or head["mode"] != "MAKER":
            return False
        for row in rows:
            if row["side"] not in {"YES", "NO"} or float(row["weight"]) <= 0:
                return False
            if int(row["created_ts"]) != created or int(row["execution_deadline_ts"]) != deadline:
                return False
    except (ValueError, TypeError):
        return False
    return True


def merge(inputs: list[Path], output: Path, now: int, min_edge: float, max_age: int, max_bundles: int) -> tuple[int, int]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in inputs:
        for row in load_file(path):
            grouped[row["bundle_id"]].append(row)
    bundles: list[list[dict[str, str]]] = []
    rejected = 0
    for rows in grouped.values():
        if valid_bundle(rows, now, min_edge, max_age):
            bundles.append(rows)
        else:
            rejected += 1
    bundles.sort(key=lambda rows: (float(rows[0]["expected_edge"]), int(rows[0]["created_ts"])), reverse=True)
    bundles = bundles[:max(0, max_bundles)]
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for rows in bundles:
                writer.writerows(rows)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return len(bundles), rejected


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge V7 bundle intents")
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-edge", type=float, default=0.001)
    parser.add_argument("--max-age-seconds", type=int, default=600)
    parser.add_argument("--max-bundles", type=int, default=20)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()
    now = int(time.time()) if args.now is None else args.now
    kept, rejected = merge(args.input, args.output, now, args.min_edge, args.max_age_seconds, args.max_bundles)
    print(f"v7_intent_merge kept_bundles={kept} rejected_bundles={rejected} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
