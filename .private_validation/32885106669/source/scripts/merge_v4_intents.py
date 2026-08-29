#!/usr/bin/env python3
"""Merge B1/B2 intent CSVs into an atomic broker input file.

The unit of admission is a complete bundle: if one row of a bundle is stale or
malformed the whole bundle is dropped. This prevents the broker from seeing an
incomplete hedge because one scanner file was partially written/read.
"""
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
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if r.fieldnames is None or any(x not in r.fieldnames for x in FIELDS):
            raise ValueError(f"{path}: incompatible intent schema")
        return [{k: (row.get(k) or "").strip() for k in FIELDS} for row in r]


def valid_bundle(rows: list[dict[str, str]], now: int, min_edge: float, max_age: int) -> bool:
    if len(rows) < 2:
        return False
    h = rows[0]
    if any(r["bundle_id"] != h["bundle_id"] or r["strategy"] != h["strategy"] for r in rows):
        return False
    if len({(r["market_id"], r["side"]) for r in rows}) != len(rows):
        return False
    try:
        created = int(h["created_ts"])
        deadline = int(h["execution_deadline_ts"])
        hold = int(h["hold_deadline_ts"])
        edge = float(h["expected_edge"])
        notional = float(h["max_notional"])
        if created > now + 5 or now - created > max_age or deadline <= now or hold <= deadline:
            return False
        if edge < min_edge or notional <= 0 or h["mode"] != "MAKER":
            return False
        for r in rows:
            if r["side"] not in {"YES", "NO"} or float(r["weight"]) <= 0:
                return False
            if int(r["created_ts"]) != created or int(r["execution_deadline_ts"]) != deadline:
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
    bundles.sort(key=lambda rs: (float(rs[0]["expected_edge"]), int(rs[0]["created_ts"])), reverse=True)
    bundles = bundles[:max(0, max_bundles)]

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for rows in bundles:
                for row in rows:
                    w.writerow(row)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, output)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return len(bundles), rejected


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", action="append", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--min-edge", type=float, default=0.001)
    ap.add_argument("--max-age-seconds", type=int, default=600)
    ap.add_argument("--max-bundles", type=int, default=20)
    ap.add_argument("--now", type=int, default=None, help="test override")
    args = ap.parse_args()
    now = int(time.time()) if args.now is None else args.now
    kept, rejected = merge(args.input, args.output, now, args.min_edge, args.max_age_seconds, args.max_bundles)
    print(f"intent_merge kept_bundles={kept} rejected_bundles={rejected} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
