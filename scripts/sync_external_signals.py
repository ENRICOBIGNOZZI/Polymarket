#!/usr/bin/env python3
"""Convert fresh external-intelligence observations into live paper signals.

Only direct probability observations are eligible. Feature-only rows from news or
crypto feeds remain research data until a separately validated forecasting model
exists. Probabilities are shrunk toward the contemporaneous Polymarket midpoint
by freshness, mapping quality and source confidence before reaching the paper
engine. The output format is the engine's atomic CSV contract.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

FIELDS = ("market_key", "q_yes", "confidence", "source", "timestamp")


@dataclass(frozen=True)
class Signal:
    market_key: str
    q_yes: float
    confidence: float
    source: str
    timestamp: int
    score: float


def finite(value: Any, default: float = math.nan) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def build_signal(
    row: dict[str, Any],
    *,
    now: int,
    max_age_seconds: int,
    max_source_age_seconds: int,
    min_confidence: float,
    min_mapping_score: float,
    shrink_strength: float,
    max_probability_gap: float,
) -> Signal | None:
    if str(row.get("feature_name") or "") != "external_probability":
        return None
    market_key = str(row.get("market_id") or "").strip()
    if not market_key:
        return None

    timestamp = integer(row.get("observed_ts") or row.get("retrieved_ts"))
    source_event_ts = integer(row.get("source_event_ts"), timestamp)
    if timestamp <= 0 or timestamp > now + 60:
        return None
    observation_age = max(0, now - timestamp)
    source_age = max(0, timestamp - source_event_ts) if source_event_ts > 0 else 0
    if observation_age > max_age_seconds or source_age > max_source_age_seconds:
        return None

    q_external = finite(row.get("q_external"))
    pm_mid = finite(row.get("pm_mid"))
    confidence = finite(row.get("confidence"), 0.0)
    mapping_score = finite(row.get("mapping_score"), 0.0)
    if not (0.0 < q_external < 1.0 and 0.0 < pm_mid < 1.0):
        return None
    if confidence < min_confidence or mapping_score < min_mapping_score:
        return None
    if abs(q_external - pm_mid) > max_probability_gap:
        return None

    freshness = math.exp(-math.log(2.0) * observation_age / max(1.0, max_age_seconds / 2.0))
    source_freshness = math.exp(
        -math.log(2.0) * source_age / max(1.0, max_source_age_seconds / 2.0)
    )
    effective_confidence = min(
        1.0,
        max(0.0, confidence) * max(0.0, mapping_score) * freshness * source_freshness,
    )
    if effective_confidence < min_confidence * 0.35:
        return None

    shrink = min(1.0, max(0.0, shrink_strength) * effective_confidence)
    q_yes = min(0.999, max(0.001, pm_mid + shrink * (q_external - pm_mid)))
    source = str(row.get("source") or "external")
    source_id = str(row.get("source_id") or "")
    label = f"external_intelligence:{source}" + (f":{source_id}" if source_id else "")
    score = effective_confidence * abs(q_yes - pm_mid)
    return Signal(market_key, q_yes, effective_confidence, label, timestamp, score)


def atomic_csv(path: Path, signals: Iterable[Signal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for signal in signals:
            writer.writerow(
                {
                    "market_key": signal.market_key,
                    "q_yes": f"{signal.q_yes:.12g}",
                    "confidence": f"{signal.confidence:.12g}",
                    "source": signal.source,
                    "timestamp": signal.timestamp,
                }
            )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--now", type=int, default=None)
    parser.add_argument("--max-age-seconds", type=int, default=21600)
    parser.add_argument("--max-source-age-seconds", type=int, default=43200)
    parser.add_argument("--min-confidence", type=float, default=0.20)
    parser.add_argument("--min-mapping-score", type=float, default=0.50)
    parser.add_argument("--shrink-strength", type=float, default=1.35)
    parser.add_argument("--max-probability-gap", type=float, default=0.45)
    parser.add_argument("--max-signals", type=int, default=500)
    args = parser.parse_args()

    now = int(time.time()) if args.now is None else args.now
    if args.max_age_seconds <= 0 or args.max_source_age_seconds <= 0:
        raise SystemExit("freshness windows must be positive")
    if not args.input.exists():
        atomic_csv(args.output, [])
        print("external_signal_sync input=missing accepted=0")
        return 0

    best: dict[str, Signal] = {}
    rows = 0
    for row in read_jsonl(args.input):
        rows += 1
        signal = build_signal(
            row,
            now=now,
            max_age_seconds=args.max_age_seconds,
            max_source_age_seconds=args.max_source_age_seconds,
            min_confidence=args.min_confidence,
            min_mapping_score=args.min_mapping_score,
            shrink_strength=args.shrink_strength,
            max_probability_gap=args.max_probability_gap,
        )
        if signal is None:
            continue
        incumbent = best.get(signal.market_key)
        if incumbent is None or (signal.timestamp, signal.score) > (
            incumbent.timestamp,
            incumbent.score,
        ):
            best[signal.market_key] = signal

    signals = sorted(best.values(), key=lambda item: (item.score, item.timestamp), reverse=True)
    signals = signals[: max(0, args.max_signals)]
    atomic_csv(args.output, signals)
    print(
        f"external_signal_sync input={rows} accepted={len(signals)} "
        f"freshest={max((item.timestamp for item in signals), default=0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
