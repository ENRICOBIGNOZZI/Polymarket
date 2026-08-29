#!/usr/bin/env python3
"""Keep GRAPH_RV in paper research and measure its *joint-fill* EV.

The semantic/Graph scanner remains on.  Its output is deliberately written to
research artifacts only: this program has no broker-output argument and cannot
make an intent executable.  A candidate is scored only with an empirical
distribution of full, partial, and zero *basket* completions from final paper
ledger rows.  With too little evidence, it reports insufficient evidence rather
than reusing the scanner's raw spread as a tradable edge.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from execution_ev import assess_candidate


SCHEMA = "polymarket_graph_research_ev_v1"
GRAPH_STRATEGY = "GRAPH_RV"
FINAL_STATUSES = {"CLOSED", "UNWOUND", "CANCELLED"}
RESEARCH_FIELDS = [
    "bundle_id",
    "strategy",
    "event_id",
    "created_ts",
    "leg_count",
    "max_notional_usd",
    "scanner_expected_edge",
    "joint_observations",
    "p_full",
    "p_partial",
    "p_zero",
    "conditional_alpha_usd",
    "conditional_costs_usd",
    "conditional_adverse_markout_usd",
    "conditional_unwind_loss_usd",
    "capital_latency_cost_usd",
    "joint_fill_ev_usd",
    "research_state",
    "reason_codes",
]


def finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def bounded(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            return [dict(row) for row in csv.DictReader(handle) if row]
    except (OSError, csv.Error):
        return []


def atomic_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESEARCH_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in RESEARCH_FIELDS})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    p = bounded(probability, 0.0, 1.0)
    if len(ordered) == 1:
        return ordered[0]
    position = p * (len(ordered) - 1)
    left, right = int(math.floor(position)), int(math.ceil(position))
    if left == right:
        return ordered[left]
    weight = position - left
    return ordered[left] * (1.0 - weight) + ordered[right] * weight


def wilson(successes: int, trials: int, z: float = 1.6448536269514722) -> tuple[float, float]:
    """Return a central 90% Wilson interval; use it conservatively below."""
    if trials <= 0:
        return 0.0, 1.0
    n = float(trials)
    p = bounded(successes / n, 0.0, 1.0)
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denominator
    radius = z * math.sqrt(max(0.0, p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denominator
    return bounded(center - radius, 0.0, 1.0), bounded(center + radius, 0.0, 1.0)


def post_leg_counts(event_rows: Iterable[dict[str, str]]) -> dict[str, int]:
    legs: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in event_rows:
        if str(row.get("event") or "").upper() != "POST":
            continue
        bundle = str(row.get("bundle_id") or "").strip()
        market = str(row.get("market_id") or "").strip()
        side = str(row.get("side") or "").strip().upper()
        if bundle and market and side:
            legs[bundle].add((market, side))
    return {bundle: len(values) for bundle, values in legs.items()}


def finalized_graph_samples(
    ledger_rows: Iterable[dict[str, str]], event_rows: Iterable[dict[str, str]], completion_threshold: float
) -> list[dict[str, Any]]:
    counts = post_leg_counts(event_rows)
    samples: list[dict[str, Any]] = []
    for row in ledger_rows:
        if str(row.get("strategy") or "").upper() != GRAPH_STRATEGY:
            continue
        if str(row.get("status") or "").upper() not in FINAL_STATUSES:
            continue
        bundle = str(row.get("bundle_id") or "").strip()
        leg_count = counts.get(bundle, 0)
        entry = finite(row.get("entry_cash"), 0.0)
        fill = bounded(finite(row.get("fill_fraction"), 0.0), 0.0, 1.0)
        if not bundle or leg_count < 2 or entry <= 0.0:
            continue
        outcome = "full" if fill >= completion_threshold else "partial" if fill > 0.0 else "zero"
        samples.append(
            {
                "leg_count": leg_count,
                "outcome": outcome,
                "cost_ratio": max(0.0, finite(row.get("fees"), 0.0) + finite(row.get("slippage"), 0.0)) / entry,
                "adverse_ratio": max(0.0, -finite(row.get("adverse_mark_pnl"), 0.0)) / entry,
                "unwind_loss_ratio": max(0.0, -finite(row.get("net_pnl"), 0.0)) / entry if outcome == "partial" else 0.0,
            }
        )
    return samples


def build_joint_models(
    samples: Iterable[dict[str, Any]], *, cost_quantile: float
) -> dict[int, dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[int(sample["leg_count"])] .append(sample)
    models: dict[int, dict[str, Any]] = {}
    for leg_count, values in grouped.items():
        observations = len(values)
        full_count = sum(value["outcome"] == "full" for value in values)
        partial_count = sum(value["outcome"] == "partial" for value in values)
        lower_full, _ = wilson(full_count, observations)
        _, upper_partial = wilson(partial_count, observations)
        p_full = lower_full
        p_partial = min(max(0.0, upper_partial), 1.0 - p_full)
        models[leg_count] = {
            "leg_count": leg_count,
            "observations": observations,
            "full_count": full_count,
            "partial_count": partial_count,
            "zero_count": observations - full_count - partial_count,
            "p_full": p_full,
            "p_partial": p_partial,
            "p_zero": max(0.0, 1.0 - p_full - p_partial),
            "cost_ratio": quantile([value["cost_ratio"] for value in values], cost_quantile),
            "adverse_ratio": quantile([value["adverse_ratio"] for value in values], cost_quantile),
            "unwind_loss_ratio": quantile(
                [value["unwind_loss_ratio"] for value in values if value["outcome"] == "partial"],
                cost_quantile,
            ),
        }
    return models


def graph_bundles(rows: Iterable[dict[str, str]]) -> list[list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if str(row.get("strategy") or "").upper() != GRAPH_STRATEGY:
            continue
        bundle = str(row.get("bundle_id") or "").strip()
        if bundle:
            grouped[bundle].append(row)
    return [grouped[key] for key in sorted(grouped)]


def assess_bundles(
    bundles: Iterable[list[dict[str, str]]],
    models: dict[int, dict[str, Any]],
    *,
    min_observations: int,
    minimum_ev_usd: float,
    capital_latency_bps: float,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    rejections: Counter[str] = Counter()
    for legs in bundles:
        first = legs[0]
        bundle = str(first.get("bundle_id") or "")
        leg_count = len({(str(row.get("market_id") or ""), str(row.get("side") or "").upper()) for row in legs})
        notional = max(0.0, finite(first.get("max_notional"), 0.0))
        scanner_edge = finite(first.get("expected_edge"), math.nan)
        row: dict[str, Any] = {
            "bundle_id": bundle,
            "strategy": GRAPH_STRATEGY,
            "event_id": str(first.get("event_id") or ""),
            "created_ts": int(finite(first.get("created_ts"), 0.0)),
            "leg_count": leg_count,
            "max_notional_usd": notional,
            "scanner_expected_edge": scanner_edge if math.isfinite(scanner_edge) else "",
        }
        model = models.get(leg_count)
        candidate: dict[str, Any] = {
            "candidate_id": bundle,
            "leg_count": leg_count,
            "minimum_ev_usd": minimum_ev_usd,
        }
        if model is None or model["observations"] < min_observations:
            assessment = assess_candidate(candidate)
            reasons = list(assessment["reason_codes"])
            reasons.append("insufficient_empirical_joint_fill_observations")
            row.update(
                {
                    "joint_observations": 0 if model is None else model["observations"],
                    "p_full": "",
                    "p_partial": "",
                    "p_zero": "",
                    "conditional_alpha_usd": "",
                    "conditional_costs_usd": "",
                    "conditional_adverse_markout_usd": "",
                    "conditional_unwind_loss_usd": "",
                    "capital_latency_cost_usd": "",
                    "joint_fill_ev_usd": "",
                    "research_state": "RESEARCH_INSUFFICIENT_EVIDENCE",
                    "reason_codes": ";".join(sorted(set(reasons))),
                }
            )
            rejections["insufficient_empirical_joint_fill_observations"] += 1
            rows.append(row)
            continue

        alpha = max(0.0, scanner_edge) * notional
        costs = model["cost_ratio"] * notional
        adverse = model["adverse_ratio"] * notional
        unwind = model["unwind_loss_ratio"] * notional
        latency = max(0.0, capital_latency_bps) * notional / 10_000.0
        candidate.update(
            {
                "joint_completion": {
                    "full": model["p_full"],
                    "partial": model["p_partial"],
                    "zero": model["p_zero"],
                    "source": f"empirical_joint_graph_rv_legs_{leg_count}",
                    "observations": model["observations"],
                },
                "conditional_alpha_usd": alpha,
                "conditional_costs_usd": costs,
                "conditional_adverse_markout_usd": adverse,
                "conditional_unwind_loss_usd": unwind,
                "capital_latency_cost_usd": latency,
            }
        )
        assessment = assess_candidate(candidate)
        reasons = list(assessment["reason_codes"])
        # A positive research estimate remains research.  It is not a broker order.
        reasons.append("research_only_no_broker_route")
        state = "RESEARCH_ECONOMIC_CANDIDATE" if assessment["admissible"] else "RESEARCH_INSUFFICIENT_EVIDENCE"
        if not assessment["admissible"]:
            for reason in assessment["reason_codes"]:
                rejections[reason] += 1
        row.update(
            {
                "joint_observations": model["observations"],
                "p_full": model["p_full"],
                "p_partial": model["p_partial"],
                "p_zero": model["p_zero"],
                "conditional_alpha_usd": alpha,
                "conditional_costs_usd": costs,
                "conditional_adverse_markout_usd": adverse,
                "conditional_unwind_loss_usd": unwind,
                "capital_latency_cost_usd": latency,
                "joint_fill_ev_usd": assessment["expected_value_usd"] if assessment["expected_value_usd"] is not None else "",
                "research_state": state,
                "reason_codes": ";".join(sorted(set(reasons))),
            }
        )
        rows.append(row)
    return rows, rejections


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="guarded relation intents")
    parser.add_argument("--ledger", type=Path, required=True, help="final paper bundle ledger")
    parser.add_argument("--events", type=Path, required=True, help="paper multi-leg events")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--min-observations", type=int, default=30)
    parser.add_argument("--minimum-ev-usd", type=float, default=0.0)
    parser.add_argument("--capital-latency-bps", type=float, default=1.0)
    parser.add_argument("--completion-threshold", type=float, default=0.75)
    parser.add_argument("--cost-quantile", type=float, default=0.75)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args(argv)
    now = int(time.time()) if args.now is None else int(args.now)
    completion_threshold = bounded(args.completion_threshold, 0.0, 1.0)
    samples = finalized_graph_samples(read_rows(args.ledger), read_rows(args.events), completion_threshold)
    models = build_joint_models(samples, cost_quantile=args.cost_quantile)
    candidates, rejection_counts = assess_bundles(
        graph_bundles(read_rows(args.input)),
        models,
        min_observations=max(1, args.min_observations),
        minimum_ev_usd=args.minimum_ev_usd,
        capital_latency_bps=args.capital_latency_bps,
    )
    atomic_csv(args.output, candidates)
    model_rows = [models[key] for key in sorted(models)]
    status = {
        "schema": SCHEMA,
        "timestamp": now,
        "paper_only": True,
        "graph_mode": "RESEARCH_ONLY",
        "broker_routing_enabled": False,
        "raw_scanner_edge_is_execution_edge": False,
        "candidate_bundles": len(candidates),
        "economic_research_candidates": sum(row["research_state"] == "RESEARCH_ECONOMIC_CANDIDATE" for row in candidates),
        "insufficient_evidence_candidates": sum(row["research_state"] == "RESEARCH_INSUFFICIENT_EVIDENCE" for row in candidates),
        "min_joint_observations": max(1, args.min_observations),
        "joint_models": model_rows,
        "rejections": dict(sorted(rejection_counts.items())),
        "broker_intents_written": 0,
    }
    atomic_json(args.status, status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
