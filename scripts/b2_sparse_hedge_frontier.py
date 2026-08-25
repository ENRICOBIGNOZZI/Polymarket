#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


def parse_spec(value: str) -> tuple[int, Path]:
    try:
        label, path = value.split(":", 1)
        k = int(label)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("input must be MAX_HEDGES:path") from exc
    if k < 1:
        raise argparse.ArgumentTypeError("MAX_HEDGES must be positive")
    return k, Path(path)


def as_float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def as_int(row: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(float(row.get(key, "")))
    except (TypeError, ValueError):
        return default


def load_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def row_key(row: dict[str, str]) -> tuple[str, str]:
    return row.get("market", ""), row.get("side", "")


def stress_edges(row: dict[str, str]) -> dict[str, float]:
    raw = as_float(row, "raw_expected_edge")
    maker = as_float(row, "maker_entry_net_edge")
    drag = max(0.0, raw - maker)
    return {
        "maker_1x": maker,
        "maker_1_5x": raw - 1.5 * drag,
        "maker_2x": raw - 2.0 * drag,
        "execution_drag": drag,
    }


def summarize(k: int, rows: list[dict[str, str]]) -> dict[str, Any]:
    maker_edges = [as_float(row, "maker_entry_net_edge") for row in rows]
    raw_edges = [as_float(row, "raw_expected_edge") for row in rows]
    stressed = [stress_edges(row) for row in rows]
    maker_positive = [row for row in rows if as_float(row, "maker_entry_net_edge") > 0.0]
    robust_positive = [
        row
        for row in rows
        if stress_edges(row)["maker_2x"] > 0.0 and as_float(row, "hedge_error", 1.0) <= 0.80
    ]
    return {
        "max_hedges": k,
        "opportunities": len(rows),
        "raw_positive": sum(edge > 0.0 for edge in raw_edges),
        "maker_positive": len(maker_positive),
        "robust_2x_positive": len(robust_positive),
        "best_raw_edge": max(raw_edges, default=0.0),
        "best_maker_edge": max(maker_edges, default=0.0),
        "median_maker_edge": statistics.median(maker_edges) if maker_edges else 0.0,
        "best_2x_stressed_edge": max((item["maker_2x"] for item in stressed), default=0.0),
        "maker_positive_executable_notional": sum(
            max(0.0, as_float(row, "executable_notional")) for row in maker_positive
        ),
        "mean_realized_hedge_legs": (
            statistics.mean(as_int(row, "hedges") for row in rows) if rows else 0.0
        ),
    }


def compare(incumbent: list[dict[str, str]], challenger: list[dict[str, str]]) -> dict[str, Any]:
    base = {row_key(row): row for row in incumbent if all(row_key(row))}
    alt = {row_key(row): row for row in challenger if all(row_key(row))}
    common = sorted(set(base).intersection(alt))
    maker_deltas = [
        as_float(alt[key], "maker_entry_net_edge") - as_float(base[key], "maker_entry_net_edge")
        for key in common
    ]
    hedge_deltas = [
        as_float(alt[key], "hedge_error", 1.0) - as_float(base[key], "hedge_error", 1.0)
        for key in common
    ]
    newly_positive = [
        key
        for key in common
        if as_float(base[key], "maker_entry_net_edge") <= 0.0
        and as_float(alt[key], "maker_entry_net_edge") > 0.0
    ]
    robust_new = [
        key
        for key in newly_positive
        if stress_edges(alt[key])["maker_2x"] > 0.0
        and as_float(alt[key], "hedge_error", 1.0) <= 0.80
    ]
    return {
        "common_market_sides": len(common),
        "mean_maker_edge_delta": statistics.mean(maker_deltas) if maker_deltas else 0.0,
        "median_maker_edge_delta": statistics.median(maker_deltas) if maker_deltas else 0.0,
        "mean_hedge_error_delta": statistics.mean(hedge_deltas) if hedge_deltas else 0.0,
        "new_maker_positive": len(newly_positive),
        "new_robust_2x_positive": len(robust_new),
        "new_positive_keys": [f"{market}:{side}" for market, side in newly_positive[:20]],
        "robust_positive_keys": [f"{market}:{side}" for market, side in robust_new[:20]],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# B2 sparse hedge cost frontier",
        "",
        "The incumbent is the `max_hedges=8` scan. Cost stress conservatively scales the observed raw-to-maker execution drag while keeping the model-implied raw edge fixed.",
        "",
        "| Max hedge candidates | Opportunities | Raw+ | Maker+ | 2x robust+ | Best raw | Best maker | Best 2x | Mean realized legs |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["frontier"]:
        lines.append(
            "| {max_hedges} | {opportunities} | {raw_positive} | {maker_positive} | {robust_2x_positive} | {best_raw_edge:.6f} | {best_maker_edge:.6f} | {best_2x_stressed_edge:.6f} | {mean_realized_hedge_legs:.2f} |".format(**item)
        )
    lines.extend(["", "## Paired comparison to max_hedges=8", ""])
    for key, value in sorted(report["comparisons"].items(), key=lambda item: int(item[0])):
        lines.append(
            f"- max_hedges={key}: common={value['common_market_sides']}, "
            f"mean maker delta={value['mean_maker_edge_delta']:.6f}, "
            f"median maker delta={value['median_maker_edge_delta']:.6f}, "
            f"new maker-positive={value['new_maker_positive']}, "
            f"new 2x-robust-positive={value['new_robust_2x_positive']}."
        )
    lines.extend([
        "",
        "## Decision rule",
        "",
        "This is research-only. A sparse setting is not evidence-ready unless it creates positive maker edge that remains positive under the 2x execution-drag stress, respects the existing hedge-error bound, and persists in at least two later chronological public-data windows. No production threshold, risk limit, or live champion is changed by this diagnostic.",
        "",
    ])
    return "\n".join(lines)


def build_report(inputs: list[tuple[int, Path]]) -> dict[str, Any]:
    rows_by_k = {k: load_rows(path) for k, path in inputs}
    if 8 not in rows_by_k:
        raise ValueError("an incumbent input with max_hedges=8 is required")
    frontier = [summarize(k, rows_by_k[k]) for k in sorted(rows_by_k)]
    comparisons = {
        str(k): compare(rows_by_k[8], rows)
        for k, rows in sorted(rows_by_k.items())
        if k != 8
    }
    return {
        "schema": "polymarket_b2_sparse_hedge_frontier_v1",
        "incumbent_max_hedges": 8,
        "frontier": frontier,
        "comparisons": comparisons,
        "evidence_state": "MORE_EVIDENCE_REQUIRED",
        "promotion_requirements": [
            "positive maker edge",
            "positive 2x execution-drag-stressed edge",
            "hedge_error <= 0.80",
            "persistence in at least two later chronological public-data windows",
            "no weakening of portfolio or authenticated-execution safeguards",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare B2 hedge-count scans under executable-cost stress")
    parser.add_argument("--input", action="append", required=True, type=parse_spec)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown", required=True)
    args = parser.parse_args()

    report = build_report(args.input)
    Path(args.output_json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(args.output_markdown).write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
