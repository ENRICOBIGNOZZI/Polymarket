#!/usr/bin/env python3
"""Build an empirical External Fair execution-latency distribution.

Only observed timestamps are used.  Missing signing/network/ack components stay
explicitly unavailable; they are never replaced with configured constants.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics
import subprocess
import time
from typing import Any, Iterable

try:
    from v7_external_economic_common import (
        atomic_json, canonical_sha256, finite, group_trade_lifecycles,
        load_counterfactual_evidence,
    )
except ModuleNotFoundError:
    from scripts.v7_external_economic_common import (
        atomic_json, canonical_sha256, finite, group_trade_lifecycles,
        load_counterfactual_evidence,
    )


SCHEMA = "polymarket_v7_execution_latency_distribution_v1"
QUANTILES = (0.50, 0.90, 0.99)


def nearest_rank(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def distribution(values: Iterable[float]) -> dict[str, Any]:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)) and value >= 0.0)
    return {
        "n": len(clean),
        "minimum_ms": clean[0] if clean else None,
        "mean_ms": statistics.fmean(clean) if clean else None,
        "p50_ms": nearest_rank(clean, 0.50),
        "p90_ms": nearest_rank(clean, 0.90),
        "p99_ms": nearest_rank(clean, 0.99),
        "maximum_ms": clean[-1] if clean else None,
    }


def build_latency_report(
    rows: list[dict[str, Any]], quality: dict[str, Any], repository_head: str,
) -> dict[str, Any]:
    components: dict[str, list[float]] = defaultdict(list)
    by_sha: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    lifecycles = group_trade_lifecycles(rows)
    for lifecycle in lifecycles.values():
        candidate = lifecycle.get("candidate") if isinstance(lifecycle.get("candidate"), dict) else {}
        fill = lifecycle.get("fill") if isinstance(lifecycle.get("fill"), dict) else {}
        if not fill:
            continue
        sha = str(fill.get("model_sha") or "UNKNOWN")
        decision = int(finite(candidate.get("decision_ts_ms"), finite(candidate.get("timestamp_ms"), 0.0)) or 0)
        arrival = int(finite(fill.get("receive_ts_ms"), 0.0) or 0)
        exchange = int(finite(fill.get("exchange_ts_ms"), 0.0) or 0)
        decision_receive = int(finite(candidate.get("receive_ts_ms"), 0.0) or 0)
        decision_exchange = int(finite(candidate.get("exchange_ts_ms"), 0.0) or 0)
        samples = {
            "decision_to_arrival": arrival - decision if arrival >= decision > 0 else None,
            # Public book timestamps can denote the last matching-engine book
            # update, not packet send time. These deltas are book age, not
            # network or CLOB processing latency.
            "arrival_book_age": arrival - exchange if arrival >= exchange > 0 else None,
            "decision_book_age": (
                decision_receive - decision_exchange
                if decision_receive >= decision_exchange > 0 else None
            ),
        }
        for name, value in samples.items():
            if value is not None:
                components[name].append(float(value))
                by_sha[sha][name].append(float(value))
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at_unix_ms": int(time.time() * 1000),
        "repository_head": repository_head,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "source_quality": quality,
        "components": {
            name: distribution(values) for name, values in sorted(components.items())
        },
        "by_entry_sha": {
            sha: {name: distribution(values) for name, values in sorted(parts.items())}
            for sha, parts in sorted(by_sha.items())
        },
        "stress_profiles": {},
        "unobserved_components": [
            "feed_to_decision_ms", "decision_to_send_ms", "signing_ms",
            "network_ms", "clob_processing_ms", "ack_ms", "cancel_ms",
        ],
        "interpretation": {
            "configured_latency_used_as_observation": False,
            "observed_counterfactual_recheck_is_live_order_latency": False,
            "exchange_to_receive_delta_is_book_age_not_network_latency": True,
            "missing_components_are_zero": False,
        },
    }
    decision = report["components"].get("decision_to_arrival", {})
    for label in ("p50", "p90", "p99"):
        value = decision.get(f"{label}_ms")
        report["stress_profiles"][label] = {
            "decision_to_arrival_ms": value,
            "available": value is not None,
        }
    p99 = decision.get("p99_ms")
    report["stress_profiles"]["p99_plus_jitter"] = {
        "decision_to_arrival_ms": (p99 * 1.25 if p99 is not None else None),
        "jitter_multiplier": 1.25,
        "available": p99 is not None,
    }
    report["content_sha256"] = canonical_sha256(report)
    return report


def generate(inputs: Iterable[Path], repo: Path, output: Path) -> dict[str, Any]:
    rows, quality = load_counterfactual_evidence(inputs)
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()
    report = build_latency_report(rows, quality, sha)
    atomic_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = generate(args.input, args.repo.resolve(), args.output)
    print(json.dumps(report["components"], indent=2, sort_keys=True))
    return 2 if report["source_quality"]["fail_closed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
