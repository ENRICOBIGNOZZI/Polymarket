#!/usr/bin/env python3
"""Evaluate frozen executable-markout shadow predictions on future native labels.

Read-only research. Predictions must have been written before the corresponding
kind=6 label became available. No order/cancel path and no automatic promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
import gzip
import json
import math
from pathlib import Path
from typing import Any

from scripts.v7_executable_markout_forward_shadow import (
    SCHEMA as PREDICTION_SCHEMA,
    decision as parse_decision,
    native_wall_ns,
)

SCHEMA = "polymarket_v7_executable_markout_forward_evaluation_v1"
HORIZONS = (500, 1000, 2000)
ARRIVALS = (25, 50, 250, 500, 750)
PAPER = {
    "paper_only": True,
    "authenticated_execution": False,
    "real_order_submission": False,
    "real_capital_at_risk": False,
    "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
    "automatic_promotion": False,
}


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def cash_fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not all(finite(x) and x >= 0 for x in (price, rate, exponent)):
        raise ValueError("invalid fee inputs")
    amount = rate * (price * (1 - price)) ** exponent
    return float(Decimal(str(amount)).quantize(Decimal(".00001"), rounding=ROUND_HALF_UP))


def read_jsonl(path: Path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def quantiles(values: list[float]) -> dict[str, float] | None:
    values = sorted(x for x in values if finite(x))
    if not values:
        return None
    return {
        str(q): values[round((len(values) - 1) * q)]
        for q in (.05, .25, .5, .75, .95)
    }


def label_key(row: dict[str, Any]) -> tuple[Any, ...] | None:
    try:
        if (
            row.get("schema") != "polymarket_v7_native_observation_v1"
            or row.get("kind") != 6
            or row.get("paper_only") is not True
            or row.get("execution_authority") is not False
            or row.get("repricing_pair_valid") is not True
            or row.get("book_valid") is not True
        ):
            return None
        horizon = int(row["repricing_horizon_ms"])
        if horizon not in set(HORIZONS) | set(ARRIVALS):
            return None
        return (
            str(row["server_id"]), str(row["run_id"]), str(row["capture_id"]),
            str(row["market_id"]), str(row["token_id"]),
            int(row["repricing_origin_signal_version"]),
            int(row["decision_monotonic_ns"]), horizon,
        )
    except (KeyError, TypeError, ValueError):
        return None


def origin_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item["server_id"], item["run_id"], item["capture_id"],
        item["market_id"], item["token_id"], item["signal_version"],
        item["decision_monotonic_ns"],
    )


def prediction_key(row: dict[str, Any]) -> tuple[Any, ...] | None:
    try:
        return (
            str(row["server_id"]), str(row["run_id"]), str(row["capture_id"]),
            str(row["market_id"]), str(row["token_id"]),
            int(row["signal_version"]), int(row["decision_monotonic_ns"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def label_point(row: dict[str, Any]) -> dict[str, Any] | None:
    key = label_key(row)
    if key is None:
        return None
    try:
        bid = int(row["bid_e4"]) / 10000
        ask = int(row["ask_e4"]) / 10000
        quantity = float(row.get("ask_quantity") or 0) / 1_000_000
        tick = int(row["tick_e4"]) / 10000
        information_ns = native_wall_ns(row)
        if not 0 < bid < ask < 1 or quantity < 0 or tick <= 0 or information_ns <= 0:
            return None
        return {
            "key": key,
            "bid": bid,
            "ask": ask,
            "quantity": quantity,
            "tick": tick,
            "information_ns": information_ns,
            "epoch": int(row.get("connection_epoch") or 0),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def load_predictions(path: Path) -> dict[tuple[Any, ...], dict[str, Any]]:
    out = {}
    for row in read_jsonl(path):
        if (
            row.get("schema") != PREDICTION_SCHEMA
            or row.get("paper_only") is not True
            or row.get("authenticated_execution") is not False
            or row.get("real_order_submission") is not False
            or row.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
            or row.get("automatic_promotion") is not False
            or row.get("forward_eligible") is not True
        ):
            continue
        key = prediction_key(row)
        if key is None:
            continue
        prior = out.get(key)
        if prior is not None and prior != row:
            raise ValueError("conflicting forward prediction identity")
        out[key] = row
    return out


def load_native(paths: list[Path], predictions: dict[tuple[Any, ...], dict[str, Any]]):
    origins = {}
    labels = {}
    wanted = set(predictions)
    for path in paths:
        for row in read_jsonl(path):
            kind = row.get("kind")
            if kind == 2:
                item = parse_decision(row)
                if item is None:
                    continue
                key = origin_key(item)
                if key not in wanted:
                    continue
                prior = origins.get(key)
                if prior is not None and prior != row:
                    raise ValueError("conflicting origin identity")
                origins[key] = row
            elif kind == 6:
                point = label_point(row)
                if point is None:
                    continue
                key = point["key"]
                origin = key[:-1]
                if origin not in wanted:
                    continue
                prior = labels.get(key)
                if prior is not None and prior != point:
                    raise ValueError("conflicting label identity")
                labels[key] = point
    return origins, labels


def modeled_arrival_delay_ms(prediction: dict[str, Any], origin: dict[str, Any]) -> float | None:
    """Decision-to-modeled-match delay for the current PAPER contract.

    The shadow inference age is additive to the venue lifecycle delay and the
    configured transport assumption because the hypothetical order cannot be
    submitted before scoring completes.
    """
    try:
        age_ms = float(prediction.get("inference_age_ns") or 0) / 1_000_000
        venue = origin.get("paper_venue_delay_ns")
        transport = origin.get("paper_assumed_transport_delay_ns")
        if not isinstance(venue, int) or venue < 0:
            return None
        if not isinstance(transport, int) or transport < 0:
            return None
        return age_ms + (venue + transport) / 1_000_000
    except (TypeError, ValueError, OverflowError):
        return None


def choose_arrival(prediction: dict[str, Any], origin: dict[str, Any],
                   labels: dict[tuple[Any, ...], dict[str, Any]],
                   key: tuple[Any, ...]) -> tuple[float | None, int | None, dict[str, Any] | None]:
    delay_ms = modeled_arrival_delay_ms(prediction, origin)
    if delay_ms is None:
        return None, None, None
    for horizon in ARRIVALS:
        if delay_ms <= horizon:
            return delay_ms, horizon, labels.get((*key, horizon))
    return delay_ms, None, None


def evaluate(predictions: dict[tuple[Any, ...], dict[str, Any]],
             origins: dict[tuple[Any, ...], dict[str, Any]],
             labels: dict[tuple[Any, ...], dict[str, Any]]) -> dict[str, Any]:
    cells = defaultdict(lambda: {
        "predictions": 0, "origin_joined": 0, "labels": 0,
        "prediction_threshold": 0, "live_geometry": 0,
        "arrival_available": 0, "simulated_fills": 0, "marked_fills": 0,
        "positive_markout_fills": 0, "markout": 0.0,
        "prediction_errors": [], "inference_age_ms": [], "modeled_arrival_delay_ms": [],
        "fill_sizes": [], "arrival_after_target": 0, "arrival_delay_unavailable": 0,
    })
    for key, pred in sorted(predictions.items(), key=lambda item: int(item[1]["scored_wall_ns"])):
        asset = str(pred.get("asset") or "UNKNOWN")
        origin = origins.get(key)
        predictions_map = pred.get("predictions") if isinstance(pred.get("predictions"), dict) else {}
        for horizon in HORIZONS:
            cell = cells[(asset, horizon)]
            cell["predictions"] += 1
            cell["inference_age_ms"].append(float(pred.get("inference_age_ns") or 0) / 1_000_000)
            if origin is None:
                continue
            cell["origin_joined"] += 1
            target = labels.get((*key, horizon))
            try:
                forecast = float(predictions_map[str(horizon)])
            except (KeyError, TypeError, ValueError):
                continue
            if target is not None:
                scored = int(pred["scored_wall_ns"])
                if scored >= int(target["information_ns"]):
                    raise ValueError("prediction was not strictly before label availability")
                entry_ask = int(origin["ask_e4"]) / 10000
                rate = float(origin["fee_rate"])
                exponent = float(origin["fee_exponent"])
                actual_target = (
                    target["bid"] - entry_ask
                    - cash_fee_per_share(entry_ask, rate, exponent)
                    - cash_fee_per_share(target["bid"], rate, exponent)
                )
                cell["labels"] += 1
                cell["prediction_errors"].append(forecast - actual_target)
            if forecast < .01:
                continue
            cell["prediction_threshold"] += 1
            tte = int(origin["close_monotonic_ns"]) - int(origin["decision_monotonic_ns"])
            decision_ask = int(origin["ask_e4"]) / 10000
            decision_depth = float(origin.get("ask_quantity") or 0) / 1_000_000
            minimum = float(origin["minimum_order_microunits"]) / 1_000_000
            if not (
                105_000_000_000 <= tte <= 120_000_000_000
                and decision_ask <= .75
                and decision_depth + 1e-12 >= 5.0
                and minimum <= 5.0 + 1e-12
            ):
                continue
            cell["live_geometry"] += 1
            modeled_delay_ms, arrival_horizon, arrival = choose_arrival(pred, origin, labels, key)
            if modeled_delay_ms is None:
                cell["arrival_delay_unavailable"] += 1
                continue
            cell["modeled_arrival_delay_ms"].append(modeled_delay_ms)
            if modeled_delay_ms >= horizon:
                cell["arrival_after_target"] += 1
                continue
            if arrival is None or arrival_horizon is None or arrival_horizon >= horizon:
                continue
            if int(pred["scored_wall_ns"]) >= int(arrival["information_ns"]):
                # An arrival snapshot already known before scoring cannot be a
                # valid latency proxy for this forward prediction.
                continue
            cell["arrival_available"] += 1
            tick = int(origin["tick_e4"]) / 10000
            limit = min(.75, decision_ask + 2 * tick)
            if arrival["ask"] > limit:
                continue
            filled = min(5.0, arrival["quantity"])
            if filled <= 0:
                continue
            cell["simulated_fills"] += 1
            cell["fill_sizes"].append(filled)
            target = labels.get((*key, horizon))
            if target is None:
                continue
            rate = float(origin["fee_rate"])
            exponent = float(origin["fee_exponent"])
            entry_fee = cash_fee_per_share(arrival["ask"], rate, exponent) * filled
            exit_fee = cash_fee_per_share(target["bid"], rate, exponent) * filled
            markout = filled * (target["bid"] - arrival["ask"]) - entry_fee - exit_fee
            cell["marked_fills"] += 1
            cell["positive_markout_fills"] += int(markout > 0)
            cell["markout"] += markout

    output = {}
    for (asset, horizon), cell in sorted(cells.items()):
        errors = cell.pop("prediction_errors")
        ages = cell.pop("inference_age_ms")
        modeled_delays = cell.pop("modeled_arrival_delay_ms")
        sizes = cell.pop("fill_sizes")
        cell["mae"] = sum(abs(x) for x in errors) / len(errors) if errors else None
        cell["bias"] = sum(errors) / len(errors) if errors else None
        cell["inference_age_ms_quantiles"] = quantiles(ages)
        cell["modeled_arrival_delay_ms_quantiles"] = quantiles(modeled_delays)
        cell["fill_size_quantiles"] = quantiles(sizes)
        cell["markout_per_marked_fill"] = (
            cell["markout"] / cell["marked_fills"] if cell["marked_fills"] else None
        )
        output[f"{asset}:{horizon}"] = cell

    return {
        "schema": SCHEMA,
        **PAPER,
        "prediction_threshold_per_share": .01,
        "live_geometry": {
            "tte_seconds": [105, 120], "maximum_entry_price": .75,
            "target_shares": 5.0, "require_full_visible_decision_depth": True,
            "arrival_rule": "inference_age + paper_venue_delay + paper_transport_delay; first native as-of label at or after modeled arrival, strictly before markout horizon",
            "limit_rule": "min(0.75, decision_ask + 2*tick)",
        },
        "prediction_count": len(predictions),
        "origin_join_count": len(origins),
        "native_label_count": len(labels),
        "cells": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--native", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    predictions = load_predictions(args.predictions)
    origins, labels = load_native(args.native, predictions)
    result = evaluate(predictions, origins, labels)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({
        "prediction_count": result["prediction_count"],
        "origin_join_count": result["origin_join_count"],
        "native_label_count": result["native_label_count"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
