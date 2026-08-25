#!/usr/bin/env python3
"""Materialize direct external probabilities into the CSV consumed by the V5 paper engine.

Only observations that already contain a numeric q_external are admitted. Raw features such
as returns, volatility, or news counts are deliberately ignored until an external model has
converted them into an event probability. This keeps the paper feed aggressive without
inventing probabilities from non-probabilistic telemetry.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _key(row: dict[str, Any]) -> str:
    for field in ("market_id", "condition_id", "slug"):
        value = row.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def materialize(
    input_path: Path,
    output_path: Path,
    *,
    min_confidence: float,
    max_age_seconds: int,
    now: int,
) -> int:
    selected: dict[str, tuple[float, int, float, str]] = {}

    if input_path.exists():
        for raw in input_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue

            key = _key(row)
            q = _number(row.get("q_external"))
            confidence = _number(row.get("confidence"))
            timestamp = _integer(row.get("observed_ts"))
            if timestamp is None:
                timestamp = _integer(row.get("retrieved_ts"))
            if not key or q is None or confidence is None or timestamp is None:
                continue
            if not (0.0 < q < 1.0) or confidence < min_confidence:
                continue
            age = max(0, now - timestamp)
            if max_age_seconds >= 0 and age > max_age_seconds:
                continue

            mapping = _number(row.get("mapping_score"))
            if mapping is not None:
                confidence *= min(1.0, max(0.0, mapping))
            if confidence < min_confidence:
                continue

            source = str(row.get("source") or "external").strip() or "external"
            candidate = (confidence, timestamp, q, source)
            incumbent = selected.get(key)
            if incumbent is None or (candidate[0], candidate[1]) > (incumbent[0], incumbent[1]):
                selected[key] = candidate

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["market_key", "q_yes", "confidence", "source", "timestamp"])
        for key in sorted(selected):
            confidence, timestamp, q, source = selected[key]
            writer.writerow([key, f"{q:.12g}", f"{confidence:.12g}", source, timestamp])

    return len(selected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-confidence", type=float, default=0.05)
    parser.add_argument("--max-age-seconds", type=int, default=86400)
    parser.add_argument("--now", type=int, default=None, help="Override current epoch seconds for deterministic tests")
    args = parser.parse_args()

    now = int(time.time()) if args.now is None else args.now
    count = materialize(
        args.input,
        args.output,
        min_confidence=max(0.0, args.min_confidence),
        max_age_seconds=args.max_age_seconds,
        now=now,
    )
    print(f"external_paper_signals={count} input={args.input} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
