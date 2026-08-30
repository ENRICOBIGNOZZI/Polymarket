#!/usr/bin/env python3
"""Deduplicated cross-run economic audit for V7 PAPER evidence.

Archives can overlap at retention boundaries, while old C++ runtimes reused
record_id counters after a SHA cutover. This tool deduplicates by the durable
(model_sha, record_id) identity and reports legacy cross-SHA ID collisions.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

EXTERNAL = "CRYPTO_INFORMED_TAKER"
MAKER = "MICRO_MAKER_PRO"
MARKOUT_HORIZONS = ("1s", "5s", "10s", "15s", "30s", "45s", "60s", "300s")


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def ledger_paths(inputs: Iterable[Path]) -> list[Path]:
    paths: set[Path] = set()
    for source in inputs:
        if source.is_file():
            paths.add(source.resolve())
            continue
        if not source.exists():
            continue
        for pattern in ("**/ledger/execution.jsonl", "**/execution-*.jsonl.gz"):
            paths.update(path.resolve() for path in source.glob(pattern) if path.is_file())
    return sorted(paths)


def counterfactual_paths(inputs: Iterable[Path]) -> list[Path]:
    paths: set[Path] = set()
    for source in inputs:
        if source.is_file() and source.name == "counterfactuals.jsonl":
            paths.add(source.resolve())
        elif source.is_dir():
            paths.update(
                path.resolve() for path in source.glob("**/external_fair/counterfactuals.jsonl")
                if path.is_file()
            )
    return sorted(paths)


def rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                yield line_number, {"__malformed__": True}
                continue
            yield line_number, value if isinstance(value, dict) else {"__malformed__": True}


def load_unique(inputs: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = ledger_paths(inputs)
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    record_shas: dict[str, set[str]] = defaultdict(set)
    raw = malformed = missing_ids = conflicts = duplicates = 0
    for path in paths:
        for _line_number, value in rows(path):
            raw += 1
            if value.get("__malformed__"):
                malformed += 1
                continue
            record_id = str(value.get("record_id") or "")
            if not record_id:
                missing_ids += 1
                continue
            model_sha = str(value.get("model_sha") or "")
            key = (model_sha, record_id)
            record_shas[record_id].add(model_sha)
            prior = unique.get(key)
            if prior is None:
                unique[key] = value
            else:
                duplicates += 1
                if prior != value:
                    conflicts += 1
    ordered = sorted(unique.values(), key=lambda row: (int(row.get("recorded_ts_ms") or 0), str(row["record_id"])))
    legacy_collisions = sum(len(shas) > 1 for shas in record_shas.values())
    quality = {
        "ledger_files": len(paths), "raw_records": raw, "unique_records": len(ordered),
        "duplicate_records_removed": duplicates, "conflicting_duplicate_record_ids": conflicts,
        "legacy_record_id_collisions_across_sha": legacy_collisions,
        "malformed_records": malformed, "records_missing_record_id": missing_ids,
        "fail_closed": conflicts > 0 or malformed > 0 or missing_ids > 0,
    }
    return ordered, quality


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def score(probabilities: list[tuple[float, float]]) -> dict[str, Any]:
    if not probabilities:
        return {"n": 0, "brier": None, "log_loss": None}
    clipped = [(min(1.0 - 1e-12, max(1e-12, p)), y) for p, y in probabilities]
    return {
        "n": len(clipped),
        "brier": mean([(p - y) ** 2 for p, y in clipped]),
        "log_loss": mean([-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)) for p, y in clipped]),
    }


def _mean_ci95(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "lower": None, "upper": None}
    center = mean(values)
    if len(values) < 2 or center is None:
        return {"mean": center, "lower": None, "upper": None}
    half = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return {"mean": center, "lower": center - half, "upper": center + half}


def clustered_score(observations: list[tuple[float, float, str]]) -> dict[str, Any]:
    """Equal-weight settlement clusters; multiple TTEs are not independent n."""
    if not observations:
        return {
            "n": 0, "independent_contracts": 0, "brier": None,
            "log_loss": None, "brier_cluster_ci95": _mean_ci95([]),
            "log_loss_cluster_ci95": _mean_ci95([]),
            "calibration_error": None, "sharpness": None,
        }
    clusters: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for probability, actual, cluster in observations:
        p = min(1.0 - 1e-12, max(1e-12, probability))
        clusters[cluster or "UNKNOWN"].append((p, actual))
    brier_clusters = [
        sum((p - y) ** 2 for p, y in rows) / len(rows)
        for rows in clusters.values()
    ]
    log_clusters = [
        sum(-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)) for p, y in rows)
        / len(rows) for rows in clusters.values()
    ]
    cluster_forecasts = [sum(p for p, _ in rows) / len(rows) for rows in clusters.values()]
    cluster_actuals = [rows[0][1] for rows in clusters.values()]
    center = mean(cluster_forecasts)
    sharpness = (
        sum((value - center) ** 2 for value in cluster_forecasts) / len(cluster_forecasts)
        if center is not None else None
    )
    return {
        "n": len(observations),
        "independent_contracts": len(clusters),
        "scoring_unit": "SETTLEMENT_MARKET_CLUSTER_EQUAL_WEIGHTED",
        "brier": mean(brier_clusters),
        "log_loss": mean(log_clusters),
        "brier_cluster_ci95": _mean_ci95(brier_clusters),
        "log_loss_cluster_ci95": _mean_ci95(log_clusters),
        "calibration_error": (
            abs(mean(cluster_forecasts) - mean(cluster_actuals))
            if cluster_forecasts else None
        ),
        "sharpness": sharpness,
    }


def paired_cluster_delta(
    rows: list[tuple[float, float, float, str]],
) -> dict[str, Any]:
    clusters: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for model_probability, market_probability, actual, cluster in rows:
        model_p = min(1.0 - 1e-12, max(1e-12, model_probability))
        market_p = min(1.0 - 1e-12, max(1e-12, market_probability))
        clusters[cluster or "UNKNOWN"].append((
            (model_p - actual) ** 2 - (market_p - actual) ** 2,
            -(actual * math.log(model_p) + (1.0 - actual) * math.log(1.0 - model_p))
            + (actual * math.log(market_p) + (1.0 - actual) * math.log(1.0 - market_p)),
        ))
    brier = [sum(value[0] for value in values) / len(values) for values in clusters.values()]
    log_loss = [sum(value[1] for value in values) / len(values) for values in clusters.values()]
    return {
        "independent_contracts": len(clusters),
        "brier_model_minus_market": _mean_ci95(brier),
        "log_loss_model_minus_market": _mean_ci95(log_loss),
        "negative_favors_model": True,
    }


def calibration(probabilities: list[tuple[float, float]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for probability, actual in probabilities:
        buckets[min(9, max(0, int(probability * 10)))].append((probability, actual))
    return [
        {
            "lower": index / 10.0, "upper": (index + 1) / 10.0,
            "n": len(values), "mean_forecast": mean([p for p, _ in values]),
            "empirical_rate": mean([y for _, y in values]),
        }
        for index, values in sorted(buckets.items())
    ]


def external_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    fills: dict[tuple[str, str], dict[str, Any]] = {}
    finals: dict[tuple[str, str], dict[str, Any]] = {}
    counts = Counter()
    for event in events:
        if event.get("strategy") != EXTERNAL:
            continue
        kind = str(event.get("event_type") or "")
        counts[kind] += 1
        key = (str(event.get("model_sha") or ""), str(event.get("position_id") or ""))
        if kind == "FILL" and key[1]:
            fills[key] = event
        elif kind == "FINAL" and key[1]:
            finals[key] = event

    model_scores: list[tuple[float, float]] = []
    market_scores: list[tuple[float, float]] = []
    pnl: list[float] = []
    expected: list[float] = []
    wins = losses = 0
    missing_model = missing_market = 0
    for key in sorted(fills.keys() & finals.keys()):
        fill, final = fills[key], finals[key]
        metadata = fill.get("metadata") if isinstance(fill.get("metadata"), dict) else {}
        held_outcome = str(metadata.get("outcome") or "").upper()
        cashflow = finite(final.get("realized_cashflow"))
        won = cashflow is not None and cashflow > 0.0
        actual_yes = 1.0 if (won == (held_outcome == "YES")) else 0.0
        final_pnl = finite(final.get("final_pnl"))
        if final_pnl is not None:
            pnl.append(final_pnl)
            wins += int(final_pnl > 0.0)
            losses += int(final_pnl <= 0.0)
        model_yes = finite(metadata.get("fair_yes"))
        if model_yes is None:
            missing_model += 1
        else:
            model_scores.append((model_yes, actual_yes))
        market_yes = finite(metadata.get("pm_mid"))
        if market_yes is None:
            ask = finite(fill.get("fill_price"))
            if ask is not None and held_outcome in {"YES", "NO"}:
                market_yes = ask if held_outcome == "YES" else 1.0 - ask
        if market_yes is None:
            missing_market += 1
        else:
            market_scores.append((market_yes, actual_yes))
        robust_net_ev = finite(metadata.get("robust_net_ev"))
        if robust_net_ev is not None:
            expected.append(robust_net_ev)

    model = score(model_scores)
    market = score(market_scores)
    return {
        "events": dict(sorted(counts.items())), "unique_fills": len(fills),
        "unique_finals": len(finals), "matched_terminal_positions": len(fills.keys() & finals.keys()),
        "wins": wins, "losses": losses, "realized_pnl": sum(pnl),
        "predicted_robust_net_ev": sum(expected),
        "model_score": model, "market_benchmark_score": market,
        "model_brier_minus_market": (
            model["brier"] - market["brier"]
            if model["brier"] is not None and market["brier"] is not None else None
        ),
        "model_reliability": calibration(model_scores),
        "missing_model_forecasts": missing_model, "missing_market_benchmarks": missing_market,
        "execution_gate": "BLOCK_PAPER_EXECUTION_UNTIL_OOS_BENCHMARKS_PASS",
    }


def maker_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    orders: set[str] = set()
    fills: set[str] = set()
    finals: set[str] = set()
    pnl: list[float] = []
    markouts: dict[str, list[float]] = defaultdict(list)
    for event in events:
        if event.get("strategy") != MAKER:
            continue
        kind = str(event.get("event_type") or "")
        counts[kind] += 1
        if kind == "ORDER_SUBMITTED" and event.get("order_id"):
            orders.add(str(event["order_id"]))
        if kind == "FILL" and event.get("fill_id"):
            fills.add(str(event["fill_id"]))
        if kind == "FINAL":
            terminal = str(event.get("position_id") or event.get("fill_id") or event.get("record_id"))
            finals.add(terminal)
            value = finite(event.get("final_pnl"))
            if value is not None:
                pnl.append(value)
        if kind == "MARKOUT" and isinstance(event.get("markouts"), dict):
            for horizon in MARKOUT_HORIZONS:
                value = finite(event["markouts"].get(horizon))
                if value is not None:
                    markouts[horizon].append(value)
    return {
        "events": dict(sorted(counts.items())), "unique_order_ids": len(orders),
        "unique_fill_ids": len(fills), "unique_terminal_units": len(finals),
        "fill_per_unique_order": len(fills) / len(orders) if orders else None,
        "realized_pnl": sum(pnl),
        "positive_finals": sum(value > 0.0 for value in pnl),
        "nonpositive_finals": sum(value <= 0.0 for value in pnl),
        "markout_per_share": {
            horizon: {"n": len(markouts[horizon]), "mean": mean(markouts[horizon])}
            for horizon in MARKOUT_HORIZONS
        },
        "promotion_gate": "MORE_EVIDENCE_REQUIRED",
    }


def counterfactual_metrics(inputs: Iterable[Path]) -> dict[str, Any]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    malformed = conflicts = duplicates = raw = 0
    paths = counterfactual_paths(inputs)
    for path in paths:
        for _line_number, value in rows(path):
            raw += 1
            if value.get("__malformed__") or not value.get("record_id"):
                malformed += 1
                continue
            key = (str(value.get("model_sha") or ""), str(value["record_id"]))
            prior = unique.get(key)
            if prior is None:
                unique[key] = value
            else:
                duplicates += 1
                conflicts += int(prior != value)
    counts = Counter(str(value.get("event_type") or "UNKNOWN") for value in unique.values())
    model_scores: list[tuple[float, float, str]] = []
    hybrid_scores: list[tuple[float, float, str]] = []
    market_scores: list[tuple[float, float, str]] = []
    paired_scores: list[tuple[float, float, float, str]] = []
    tte_scores: dict[str, dict[str, list[tuple[float, float, str]]]] = defaultdict(
        lambda: {"model": [], "hybrid": [], "market": []})
    virtual_pnl: list[float] = []
    virtual_fills: dict[str, dict[str, Any]] = {}
    for value in unique.values():
        if value.get("event_type") == "FORECAST_FINAL":
            model, market, actual = (
                finite(value.get("model_yes")), finite(value.get("market_yes")),
                finite(value.get("actual_yes")),
            )
            hybrid = finite(value.get("hybrid_yes"))
            cluster = str(value.get("market_id") or value.get("contract_id") or "UNKNOWN")
            tte = str(int(finite(value.get("tte_bucket_seconds")) or 0))
            if model is not None and actual is not None:
                model_scores.append((model, actual, cluster))
                tte_scores[tte]["model"].append((model, actual, cluster))
            if market is not None and actual is not None:
                market_scores.append((market, actual, cluster))
                tte_scores[tte]["market"].append((market, actual, cluster))
            if hybrid is not None and actual is not None:
                hybrid_scores.append((hybrid, actual, cluster))
                tte_scores[tte]["hybrid"].append((hybrid, actual, cluster))
            if model is not None and market is not None and actual is not None:
                paired_scores.append((model, market, actual, cluster))
        if value.get("event_type") == "VIRTUAL_FILL" and value.get("fill_id"):
            virtual_fills[str(value["fill_id"])] = value
        if value.get("event_type") == "VIRTUAL_FINAL":
            pnl = finite(value.get("counterfactual_pnl"))
            if pnl is not None:
                virtual_pnl.append(pnl)
    cost_stress = {"1.0x": 0.0, "1.5x": 0.0, "2.0x": 0.0}
    matched_cost_finals = 0
    for value in unique.values():
        if value.get("event_type") != "VIRTUAL_FINAL":
            continue
        pnl = finite(value.get("counterfactual_pnl"))
        fill = virtual_fills.get(str(value.get("fill_id") or ""))
        if pnl is None or fill is None:
            continue
        base_cost = max(0.0, finite(fill.get("fee")) or 0.0) + max(
            0.0, finite(fill.get("slippage")) or 0.0)
        for multiplier in (1.0, 1.5, 2.0):
            cost_stress[f"{multiplier:.1f}x"] += pnl - (multiplier - 1.0) * base_cost
        matched_cost_finals += 1
    return {
        "tape_files": len(paths), "raw_records": raw, "unique_records": len(unique),
        "duplicates_removed": duplicates, "conflicts": conflicts, "malformed": malformed,
        "events": dict(sorted(counts.items())),
        "forecast_model_score": clustered_score(model_scores),
        "forecast_hybrid_score": clustered_score(hybrid_scores),
        "forecast_market_benchmark_score": clustered_score(market_scores),
        "model_minus_market_cluster_robust": paired_cluster_delta(paired_scores),
        "scores_by_tte_bucket_seconds": {
            bucket: {
                name: clustered_score(values)
                for name, values in groups.items()
            }
            for bucket, groups in sorted(tte_scores.items(), key=lambda item: int(item[0]))
        },
        "virtual_realized_pnl": sum(virtual_pnl),
        "virtual_cost_stress_pnl": cost_stress,
        "virtual_cost_stress_matched_finals": matched_cost_finals,
        "virtual_fill_capacity": {
            "fills": len(virtual_fills),
            "filled_shares": sum(max(0.0, finite(row.get("filled_size")) or 0.0)
                                 for row in virtual_fills.values()),
            "filled_notional": sum(
                max(0.0, finite(row.get("filled_size")) or 0.0)
                * max(0.0, finite(row.get("fill_price")) or 0.0)
                for row in virtual_fills.values()),
        },
        "fail_closed": malformed > 0 or conflicts > 0,
    }


def strategy_economics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Report every strategy while keeping SHADOW economics out of PAPER PnL."""
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "events": Counter(), "paper_terminal_units": 0, "shadow_terminal_units": 0,
        "paper_realized_pnl": 0.0, "shadow_counterfactual_pnl": 0.0,
        "capital_hours": 0.0, "markouts": defaultdict(list),
    })
    for event in events:
        strategy = str(event.get("strategy") or "UNKNOWN")
        row = grouped[strategy]
        kind = str(event.get("event_type") or "UNKNOWN")
        row["events"][kind] += 1
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        shadow = bool(
            metadata.get("counterfactual") is True
            or metadata.get("excluded_from_portfolio_equity") is True
            or str(metadata.get("economic_authority") or "PAPER").upper() in {"SHADOW", "RESEARCH", "SHADOW_COUNTERFACTUAL"}
        )
        row["capital_hours"] += max(0.0, finite(event.get("capital_hours")) or 0.0)
        if kind == "FINAL":
            pnl = finite(event.get("final_pnl"))
            if pnl is not None:
                if shadow:
                    row["shadow_terminal_units"] += 1
                    row["shadow_counterfactual_pnl"] += pnl
                else:
                    row["paper_terminal_units"] += 1
                    row["paper_realized_pnl"] += pnl
        if kind == "MARKOUT" and isinstance(event.get("markouts"), dict):
            for horizon in MARKOUT_HORIZONS:
                value = finite(event["markouts"].get(horizon))
                if value is not None:
                    row["markouts"][horizon].append(value)
    output: dict[str, Any] = {}
    for strategy, row in sorted(grouped.items()):
        output[strategy] = {
            "events": dict(sorted(row["events"].items())),
            "paper_terminal_units": row["paper_terminal_units"],
            "shadow_terminal_units": row["shadow_terminal_units"],
            "paper_realized_pnl": row["paper_realized_pnl"],
            "shadow_counterfactual_pnl": row["shadow_counterfactual_pnl"],
            "capital_hours": row["capital_hours"],
            "markout_per_share": {
                horizon: {"n": len(row["markouts"][horizon]), "mean": mean(row["markouts"][horizon])}
                for horizon in MARKOUT_HORIZONS
            },
        }
    return output


def audit(inputs: Iterable[Path]) -> dict[str, Any]:
    inputs = list(inputs)
    events, quality = load_unique(inputs)
    counterfactual = counterfactual_metrics(inputs)
    quality["fail_closed"] = bool(quality["fail_closed"] or counterfactual["fail_closed"])
    strategies = Counter(str(event.get("strategy") or "UNKNOWN") for event in events)
    external = external_metrics(events)
    maker = maker_metrics(events)
    all_strategies = strategy_economics(events)
    total_realized = float(external["realized_pnl"]) + float(maker["realized_pnl"])
    return {
        "schema": "polymarket_v7_profitability_audit_v1",
        "scope": "PAPER_ONLY_DEDUPLICATED_CROSS_RUN",
        "data_quality": quality,
        "strategy_record_counts": dict(sorted(strategies.items())),
        "external_fair": external, "professional_maker": maker,
        "external_fair_counterfactual": counterfactual,
        "strategy_economics": all_strategies,
        "selected_sleeves_realized_pnl": total_realized,
        "profitability_proven": bool(
            not quality["fail_closed"] and total_realized > 0.0
            and external["execution_gate"] != "BLOCK_PAPER_EXECUTION_UNTIL_OOS_BENCHMARKS_PASS"
            and maker["unique_terminal_units"] >= 1000
        ),
        "limitations": [
            "PAPER fills are not real fills and do not prove deployable profit.",
            "Historical rows without pm_mid use the executed-side ask as the market benchmark.",
            "Policy version is unavailable where older ledger rows did not record it.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.input)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 2 if report["data_quality"]["fail_closed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
