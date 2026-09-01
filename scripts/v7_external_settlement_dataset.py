#!/usr/bin/env python3
"""Build a causal BTC 5m settlement-margin dataset from V7 SHADOW tapes.

Features are joined by local receive time.  Initial and terminal Chainlink
values are bound to the verified contract boundaries.  The published 60-second
TWAP is available, but its raw window constituents are not; the K_t/U_t
decomposition therefore remains explicitly unavailable rather than estimated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any, Iterable, Iterator

try:
    from v7_external_economic_common import (
        atomic_json, canonical_sha256, discover_counterfactual_tapes, file_sha256,
        finite, jsonl_rows,
    )
except ModuleNotFoundError:
    from scripts.v7_external_economic_common import (
        atomic_json, canonical_sha256, discover_counterfactual_tapes, file_sha256,
        finite, jsonl_rows,
    )


SCHEMA = "polymarket_v7_external_settlement_dataset_v1"
ROW_SCHEMA = "polymarket_v7_external_settlement_row_v1"
FEATURE_SCHEMA = "btc-5m-settlement-margin-causal-v1"
ORACLE_TOPIC = "crypto_prices_twap_sixty"
EXTERNAL_TOPIC = "crypto_prices"
BOUNDARY_GAP_MS = 2_000
FEATURE_NAMES = (
    "tte_seconds",
    "terminal_window_observed_fraction",
    "oracle_minus_reference_bps",
    "external_minus_oracle_bps",
    "external_return_1s",
    "external_return_5s",
    "external_return_30s",
    "external_realized_vol_30s",
    "oracle_age_ms",
    "external_age_ms",
    "market_yes",
    "market_spread",
)
MODEL_FEATURE_NAMES = (
    "tte_seconds",
    "terminal_window_observed_fraction",
    "oracle_minus_reference_bps",
    "external_minus_oracle_bps",
    "external_return_1s",
    "external_return_5s",
    "oracle_age_ms",
    "external_age_ms",
)


def _discover(inputs: Iterable[Path], filename: str) -> list[Path]:
    paths: set[Path] = set()
    for raw in inputs:
        source = Path(raw)
        if source.is_file() and source.name == filename:
            paths.add(source.resolve())
        elif source.is_dir():
            paths.update(path.resolve() for path in source.glob(f"**/{filename}") if path.is_file())
    return sorted(paths)


def _manifest(paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [{
        "path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size,
    } for path in paths]


def load_forecasts(inputs: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = discover_counterfactual_tapes(inputs)
    unique: dict[str, dict[str, Any]] = {}
    conflicts = malformed = 0
    for path in paths:
        for _, row in jsonl_rows(path):
            if row.get("__malformed__"):
                malformed += 1
                continue
            record_id = str(row.get("record_id") or "")
            if not record_id:
                continue
            prior = unique.get(record_id)
            if prior is None:
                unique[record_id] = row
            elif prior != row:
                conflicts += 1
    origins: dict[str, dict[str, Any]] = {}
    finals: dict[str, dict[str, Any]] = {}
    for row in unique.values():
        forecast_id = str(row.get("forecast_id") or "")
        if row.get("event_type") == "FORECAST" and forecast_id:
            origins.setdefault(forecast_id, row)
        elif row.get("event_type") == "FORECAST_FINAL" and forecast_id:
            finals.setdefault(forecast_id, row)
    joined = [
        {"origin": origins[forecast_id], "final": final}
        for forecast_id, final in finals.items() if forecast_id in origins
    ]
    joined.sort(key=lambda pair: (
        int(finite(pair["origin"].get("observed_ms"), 0.0) or 0),
        str(pair["origin"].get("market_id") or ""),
        str(pair["origin"].get("forecast_id") or ""),
    ))
    return joined, {
        "counterfactual_tapes": _manifest(paths),
        "unique_records": len(unique), "joined_settled_forecasts": len(joined),
        "conflicts": conflicts, "malformed": malformed,
        "fail_closed": bool(conflicts or malformed),
    }


def load_rtds(inputs: Iterable[Path]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    paths = _discover(inputs, "rtds_events.jsonl")
    unique: dict[tuple[str, int, str], dict[str, Any]] = {}
    conflicts = malformed = 0
    for path in paths:
        for _, row in jsonl_rows(path):
            if row.get("__malformed__"):
                malformed += 1
                continue
            topic = str(row.get("topic") or "")
            source_ms = int(finite(row.get("timestamp_ms"), 0.0) or 0)
            price = finite(row.get("price"))
            receive_ns = int(finite(row.get("receive_wall_ns"), 0.0) or 0)
            if topic not in {ORACLE_TOPIC, EXTERNAL_TOPIC} or source_ms <= 0 or price is None or receive_ns <= 0:
                continue
            key = (topic, source_ms, format(price, ".15g"))
            prior = unique.get(key)
            if prior is None or receive_ns < int(prior["receive_wall_ns"]):
                unique[key] = row
            elif finite(prior.get("price")) != price:
                conflicts += 1
    output: dict[str, list[dict[str, Any]]] = {ORACLE_TOPIC: [], EXTERNAL_TOPIC: []}
    for row in unique.values():
        output[str(row["topic"])].append(row)
    for rows in output.values():
        rows.sort(key=lambda row: (
            int(row["receive_wall_ns"]), int(row["timestamp_ms"]),
        ))
    return output, {
        "rtds_tapes": _manifest(paths),
        "unique_rtds_events": len(unique), "conflicts": conflicts,
        "malformed": malformed, "fail_closed": bool(conflicts or malformed),
    }


def _right_receive_index(rows: list[dict[str, Any]], receive_ns: int) -> int:
    """Binary search without rebuilding a parallel timestamp array."""
    low, high = 0, len(rows)
    while low < high:
        middle = (low + high) // 2
        if int(rows[middle]["receive_wall_ns"]) <= receive_ns:
            low = middle + 1
        else:
            high = middle
    return low


def _latest_received(rows: list[dict[str, Any]], receive_ns: int) -> dict[str, Any] | None:
    index = _right_receive_index(rows, receive_ns)
    return rows[index - 1] if index else None


def _boundary_event(
    rows: list[dict[str, Any]], boundary_ms: int, *, received_by_ns: int | None = None,
) -> dict[str, Any] | None:
    eligible = [
        row for row in rows
        if int(row["timestamp_ms"]) <= boundary_ms
        and boundary_ms - int(row["timestamp_ms"]) <= BOUNDARY_GAP_MS
        and (received_by_ns is None or int(row["receive_wall_ns"]) <= received_by_ns)
    ]
    return max(eligible, key=lambda row: int(row["timestamp_ms"]), default=None)


def _return(rows: list[dict[str, Any]], observed_ns: int, seconds: float) -> float | None:
    current = _latest_received(rows, observed_ns)
    prior = _latest_received(rows, observed_ns - int(seconds * 1_000_000_000))
    if current is None or prior is None:
        return None
    current_price, prior_price = finite(current.get("price")), finite(prior.get("price"))
    if current_price is None or prior_price is None or min(current_price, prior_price) <= 0.0:
        return None
    return math.log(current_price / prior_price)


def _realized_vol(rows: list[dict[str, Any]], observed_ns: int, seconds: float = 30.0) -> float | None:
    start_ns = observed_ns - int(seconds * 1_000_000_000)
    start = _right_receive_index(rows, start_ns - 1)
    end = _right_receive_index(rows, observed_ns)
    window = rows[start:end]
    returns: list[float] = []
    for left, right in zip(window, window[1:]):
        first, second = finite(left.get("price")), finite(right.get("price"))
        if first is not None and second is not None and min(first, second) > 0.0:
            returns.append(math.log(second / first))
    return statistics.pstdev(returns) if len(returns) >= 2 else None


def _market_values(origin: dict[str, Any]) -> tuple[float | None, float | None]:
    market_yes = finite(origin.get("market_yes"))
    yes_bid, yes_ask = finite(origin.get("yes_best_bid")), finite(origin.get("yes_best_ask"))
    no_bid, no_ask = finite(origin.get("no_best_bid")), finite(origin.get("no_best_ask"))
    spreads = [
        ask - bid for bid, ask in ((yes_bid, yes_ask), (no_bid, no_ask))
        if bid is not None and ask is not None and ask >= bid
    ]
    return market_yes, statistics.fmean(spreads) if spreads else None


def build_row(
    pair: dict[str, dict[str, Any]], rtds: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str]:
    origin, final = pair["origin"], pair["final"]
    observed_ms = int(finite(origin.get("observed_ms"), finite(origin.get("timestamp_ms"), 0.0)) or 0)
    observed_ns = observed_ms * 1_000_000
    start_s = int(finite(origin.get("reference_version"), 0.0) or 0)
    tte = finite(origin.get("observed_tte_seconds"))
    actual_yes = finite(final.get("actual_yes"))
    if observed_ns <= 0 or start_s <= 0 or tte is None or actual_yes not in {0.0, 1.0}:
        return None, "IDENTITY_OR_LABEL_MISSING"
    end_ms = (start_s + 300) * 1000
    oracle_rows, external_rows = rtds[ORACLE_TOPIC], rtds[EXTERNAL_TOPIC]
    reference = _boundary_event(oracle_rows, start_s * 1000, received_by_ns=observed_ns)
    current_oracle = _latest_received(oracle_rows, observed_ns)
    current_external = _latest_received(external_rows, observed_ns)
    terminal = _boundary_event(oracle_rows, end_ms)
    if reference is None or current_oracle is None or terminal is None:
        return None, "REQUIRED_CAUSAL_OR_SETTLEMENT_EVENT_MISSING"
    external_features = origin.get("external_features") if isinstance(
        origin.get("external_features"), dict) else {}
    external_price = finite(external_features.get("composite_price"))
    external_return_1s = finite(external_features.get("return_1s"))
    external_return_5s = finite(external_features.get("return_5s"))
    external_age_ns = finite(external_features.get("age_ns"))
    if None in (external_price, external_return_1s, external_return_5s, external_age_ns):
        return None, "RUNTIME_EXTERNAL_FEATURES_MISSING"
    if external_age_ns < 0.0 or external_age_ns > observed_ns:
        return None, "RUNTIME_EXTERNAL_FEATURE_AGE_INVALID"
    reference_price = float(reference["price"])
    oracle_price = float(current_oracle["price"])
    external_price = float(external_price)
    terminal_price = float(terminal["price"])
    if min(reference_price, oracle_price, external_price, terminal_price) <= 0.0:
        return None, "NONPOSITIVE_PRICE"
    market_yes, market_spread = _market_values(origin)
    features = {
        "tte_seconds": tte,
        "terminal_window_observed_fraction": max(0.0, min(1.0, (60.0 - tte) / 60.0)),
        "oracle_minus_reference_bps": 10_000.0 * (oracle_price / reference_price - 1.0),
        "external_minus_oracle_bps": 10_000.0 * (external_price / oracle_price - 1.0),
        "external_return_1s": external_return_1s,
        "external_return_5s": external_return_5s,
        "external_return_30s": _return(external_rows, observed_ns, 30.0),
        "external_realized_vol_30s": _realized_vol(external_rows, observed_ns),
        "oracle_age_ms": max(0.0, observed_ms - int(current_oracle["timestamp_ms"])),
        "external_age_ms": external_age_ns / 1_000_000.0,
        "market_yes": market_yes,
        "market_spread": market_spread,
    }
    missing_model = [name for name in MODEL_FEATURE_NAMES if features.get(name) is None]
    if missing_model:
        return None, "MODEL_FEATURES_MISSING:" + ",".join(missing_model)
    missing_diagnostics = [
        name for name in FEATURE_NAMES if name not in MODEL_FEATURE_NAMES
        and features.get(name) is None
    ]
    label_margin_bps = 10_000.0 * (terminal_price / reference_price - 1.0)
    derived_yes = float(label_margin_bps >= 0.0)
    if derived_yes != actual_yes:
        return None, "PUBLIC_SETTLEMENT_LABEL_MISMATCH"
    latest_feature_receive_ns = max(
        int(reference["receive_wall_ns"]), int(current_oracle["receive_wall_ns"]),
        observed_ns - int(external_age_ns),
    )
    if latest_feature_receive_ns > observed_ns:
        return None, "CAUSALITY_FAILURE"
    row = {
        "schema": ROW_SCHEMA,
        "feature_schema": FEATURE_SCHEMA,
        "forecast_id": str(origin.get("forecast_id") or ""),
        "market_id": str(origin.get("market_id") or ""),
        "event_id": str(origin.get("event_id") or ""),
        "rules_hash": str(origin.get("rules_hash") or ""),
        "model_sha": str(origin.get("model_sha") or ""),
        "policy_sha256": str(origin.get("policy_sha256") or ""),
        "observed_ms": observed_ms,
        "observed_day": observed_ms // 86_400_000,
        "contract_start_epoch": start_s,
        "contract_end_epoch": start_s + 300,
        "latest_feature_receive_wall_ns": latest_feature_receive_ns,
        "reference_oracle": reference_price,
        "current_oracle": oracle_price,
        "current_external": external_price,
        "model_feature_source": "RECORDED_LIVE_VENUE_COMPOSITE",
        "training_serving_feature_parity": True,
        "terminal_oracle": terminal_price,
        "target_settlement_margin_bps": label_margin_bps,
        "actual_yes": actual_yes,
        "features": features,
        "missing_diagnostic_features": missing_diagnostics,
        "execution": {
            "yes_best_bid": finite(origin.get("yes_best_bid")),
            "yes_best_ask": finite(origin.get("yes_best_ask")),
            "yes_best_bid_visible_size": finite(origin.get("yes_best_bid_visible_size")),
            "yes_best_ask_visible_size": finite(origin.get("yes_best_ask_visible_size")),
            "no_best_bid": finite(origin.get("no_best_bid")),
            "no_best_ask": finite(origin.get("no_best_ask")),
            "no_best_bid_visible_size": finite(origin.get("no_best_bid_visible_size")),
            "no_best_ask_visible_size": finite(origin.get("no_best_ask_visible_size")),
            "yes_min_order_size": finite(origin.get("yes_min_order_size")),
            "no_min_order_size": finite(origin.get("no_min_order_size")),
            "fee_schedule": origin.get("fee_schedule") if isinstance(
                origin.get("fee_schedule"), dict) else {},
            "market_mid_source": origin.get("market_mid_source"),
        },
        "settlement_window_decomposition": {
            "known_component_available": False,
            "state": "RAW_TERMINAL_TWAP_CONSTITUENTS_NOT_RECORDED",
            "observed_fraction_diagnostic_only": True,
        },
        "causality_valid": True,
    }
    row["row_sha256"] = canonical_sha256(row)
    return row, "OK"


def build_dataset(inputs: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inputs = list(inputs)
    forecasts, forecast_quality = load_forecasts(inputs)
    rtds, rtds_quality = load_rtds(inputs)
    rows: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    for pair in forecasts:
        row, reason = build_row(pair, rtds)
        reasons[reason] = reasons.get(reason, 0) + 1
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: (row["observed_ms"], row["market_id"], row["forecast_id"]))
    contracts = {row["market_id"] for row in rows}
    days = {row["observed_day"] for row in rows}
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "feature_schema": FEATURE_SCHEMA,
        "feature_names": list(FEATURE_NAMES),
        "model_feature_names": list(MODEL_FEATURE_NAMES),
        "market_features_are_diagnostics_only": True,
        "rows": len(rows), "independent_contracts": len(contracts),
        "day_clusters": len(days),
        "build_reasons": dict(sorted(reasons.items())),
        "forecast_source_quality": forecast_quality,
        "rtds_source_quality": rtds_quality,
        "causality_failures": sum(1 for row in rows if row["causality_valid"] is not True),
        "settlement_window_decomposition": "UNAVAILABLE_RAW_TWAP_CONSTITUENTS",
        "fail_closed": bool(forecast_quality["fail_closed"] or rtds_quality["fail_closed"]),
    }
    manifest["dataset_sha256"] = canonical_sha256({
        "feature_schema": FEATURE_SCHEMA,
        "row_hashes": [row["row_sha256"] for row in rows],
        "source_manifests": {
            "counterfactual": forecast_quality["counterfactual_tapes"],
            "rtds": rtds_quality["rtds_tapes"],
        },
    })
    return rows, manifest


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    rows, manifest = build_dataset(args.input)
    write_jsonl(args.output, rows)
    atomic_json(args.manifest, manifest)
    print(json.dumps({
        "rows": manifest["rows"],
        "independent_contracts": manifest["independent_contracts"],
        "dataset_sha256": manifest["dataset_sha256"],
    }, sort_keys=True))
    return 2 if manifest["fail_closed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
