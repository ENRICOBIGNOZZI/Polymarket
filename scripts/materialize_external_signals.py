#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from pathlib import Path
from typing import Any

FIELDS = ("market_key", "q_yes", "confidence", "source", "timestamp")


def finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def materialize(
    input_path: Path,
    output_path: Path,
    now: int,
    min_confidence: float,
    max_age_seconds: int,
) -> int:
    """Atomically materialize fresh direct probabilities for the paper external sleeve."""
    best: dict[str, dict[str, Any]] = {}
    if input_path.exists():
        for raw in input_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            market = str(row.get("market_id") or "").strip()
            q = finite(row.get("q_external"))
            confidence = finite(row.get("confidence"), 0.0)
            timestamp = int(finite(row.get("observed_ts"), 0.0))
            age = max(0, now - timestamp)
            if not market or not (0.001 < q < 0.999):
                continue
            if confidence < min_confidence or timestamp <= 0 or age > max_age_seconds:
                continue
            current = best.get(market)
            rank = (timestamp, confidence)
            if current is None or rank > current["rank"]:
                best[market] = {
                    "rank": rank,
                    "market_key": market,
                    "q_yes": q,
                    "confidence": confidence,
                    "source": (
                        f"external_intelligence:{row.get('source', 'unknown')}:"
                        f"{row.get('source_id', '')}"
                    ),
                    "timestamp": timestamp,
                }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for market in sorted(best):
            writer.writerow({key: best[market][key] for key in FIELDS})
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output_path)
    return len(best)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-confidence", type=float, default=0.35)
    parser.add_argument("--max-age-seconds", type=int, default=21600)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()
    now = int(time.time()) if args.now is None else args.now
    count = materialize(
        args.input,
        args.output,
        now,
        max(0.0, args.min_confidence),
        max(1, args.max_age_seconds),
    )
    print(f"external_signal_materializer accepted={count} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
