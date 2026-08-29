#!/usr/bin/env python3
"""Replay trade-independent RTDS evidence by contract time-to-expiry.

The diagnostic intentionally ignores orders and fills. It requires exact
Chainlink observations at both five-minute boundaries, uses only causally
received RTDS observations for each forecast, and reports one equally weighted
observation per contract/TTE bucket. The external leg is the public RTDS
BTCUSDT stream, so this evaluates timing of the structural signal rather than
certifying the production multi-venue fair model.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path
from typing import Any, Iterable

DEFAULT_TTE_BUCKETS = (240.0, 180.0, 120.0, 90.0, 60.0, 45.0, 30.0, 20.0, 15.0, 10.0, 5.0, 2.0)
CONTRACT_SECONDS = 300
MAX_CAUSAL_OBSERVATION_AGE_NS = 2_000_000_000
ORACLE_TOPIC = "crypto_prices_twap_sixty"
EXTERNAL_TOPIC = "crypto_prices"


def structural_probability(reference: float, oracle: float, external: float, tte_seconds: float) -> float:
    if min(reference, oracle, external) <= 0.0 or tte_seconds <= 0.0:
        raise ValueError("positive prices and TTE required")
    expected_terminal = oracle + 0.70 * (external - oracle)
    sigma_fraction = math.sqrt(0.00020 ** 2 * max(1e-6, tte_seconds) + 0.00010 ** 2)
    sigma = reference * max(0.00005, sigma_fraction)
    probability = 0.5 * math.erfc(-(expected_terminal - reference) / sigma / math.sqrt(2.0))
    return min(1.0 - 1e-9, max(1e-9, probability))


def load_events(paths: Iterable[Path]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    topic = str(row["topic"])
                    receive = int(row["receive_wall_ns"])
                    source_timestamp = int(row["timestamp_ms"])
                    price = float(row["price"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"{path}:{line_number}:invalid_event") from exc
                if topic not in {ORACLE_TOPIC, EXTERNAL_TOPIC} or receive <= 0 or source_timestamp <= 0:
                    continue
                if not math.isfinite(price) or price <= 0.0:
                    continue
                key = (topic, receive, source_timestamp, price)
                unique[key] = {"topic": topic, "receive_wall_ns": receive,
                               "timestamp_ms": source_timestamp, "price": price}
    return sorted(unique.values(), key=lambda row: (row["receive_wall_ns"], row["topic"]))


def _latest_causal(rows: list[dict[str, Any]], receives: list[int], cutoff_ns: int) -> dict[str, Any] | None:
    index = bisect.bisect_right(receives, cutoff_ns) - 1
    if index < 0 or cutoff_ns - receives[index] > MAX_CAUSAL_OBSERVATION_AGE_NS:
        return None
    return rows[index]


def replay(events: Iterable[dict[str, Any]], buckets: Iterable[float] = DEFAULT_TTE_BUCKETS) -> dict[str, Any]:
    rows = sorted(events, key=lambda row: (int(row["receive_wall_ns"]), str(row["topic"])))
    oracle = [row for row in rows if row["topic"] == ORACLE_TOPIC]
    external = [row for row in rows if row["topic"] == EXTERNAL_TOPIC]
    oracle_receives = [int(row["receive_wall_ns"]) for row in oracle]
    external_receives = [int(row["receive_wall_ns"]) for row in external]
    exact: dict[int, list[dict[str, Any]]] = {}
    for row in oracle:
        exact.setdefault(int(row["timestamp_ms"]), []).append(row)
    if not rows:
        return {"schema": "polymarket_v7_external_fair_tape_replay_v1", "contracts": 0, "observations": 0,
                "buckets": [], "limitations": ["PUBLIC_RTDS_SINGLE_EXTERNAL_LEG", "NOT_EXECUTION_PROMOTION_EVIDENCE"]}

    first_second = int(rows[0]["receive_wall_ns"]) // 1_000_000_000
    last_second = int(rows[-1]["receive_wall_ns"]) // 1_000_000_000
    first_start = (first_second // CONTRACT_SECONDS) * CONTRACT_SECONDS
    metrics: dict[float, list[tuple[float, int]]] = {float(bucket): [] for bucket in buckets}
    completed_contracts: set[int] = set()
    for start in range(first_start, last_second + 1, CONTRACT_SECONDS):
        end = start + CONTRACT_SECONDS
        opening_rows, terminal_rows = exact.get(start * 1000, []), exact.get(end * 1000, [])
        if not opening_rows or not terminal_rows:
            continue
        opening = min(opening_rows, key=lambda row: int(row["receive_wall_ns"]))
        terminal = min(terminal_rows, key=lambda row: int(row["receive_wall_ns"]))
        # The verified BTC 5m contract template uses GREATER_EQUAL for YES.
        outcome = int(float(terminal["price"]) >= float(opening["price"]))
        used = False
        for bucket in metrics:
            cutoff = int((end - bucket) * 1_000_000_000)
            causal_oracle = _latest_causal(oracle, oracle_receives, cutoff)
            causal_external = _latest_causal(external, external_receives, cutoff)
            if causal_oracle is None or causal_external is None:
                continue
            if int(opening["receive_wall_ns"]) > cutoff:
                continue
            probability = structural_probability(
                float(opening["price"]), float(causal_oracle["price"]),
                float(causal_external["price"]), bucket,
            )
            metrics[bucket].append((probability, outcome))
            used = True
        if used:
            completed_contracts.add(start)

    output_buckets = []
    for bucket in metrics:
        samples = metrics[bucket]
        if not samples:
            continue
        brier = sum((probability - outcome) ** 2 for probability, outcome in samples) / len(samples)
        log_loss = -sum(
            outcome * math.log(probability) + (1 - outcome) * math.log(1.0 - probability)
            for probability, outcome in samples
        ) / len(samples)
        accuracy = sum(int((probability >= 0.5) == bool(outcome)) for probability, outcome in samples) / len(samples)
        confidence = sum(abs(probability - 0.5) for probability, _ in samples) / len(samples)
        output_buckets.append({
            "tte_seconds": bucket, "contracts": len(samples), "brier": brier,
            "log_loss": log_loss, "directional_accuracy": accuracy,
            "mean_absolute_confidence": confidence,
        })
    return {
        "schema": "polymarket_v7_external_fair_tape_replay_v1",
        "contracts": len(completed_contracts), "observations": sum(len(value) for value in metrics.values()),
        "baseline": {"probability": 0.5, "brier": 0.25, "log_loss": math.log(2.0)},
        "buckets": output_buckets,
        "limitations": ["PUBLIC_RTDS_SINGLE_EXTERNAL_LEG", "NOT_EXECUTION_PROMOTION_EVIDENCE"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tapes", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = replay(load_events(args.tapes))
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
