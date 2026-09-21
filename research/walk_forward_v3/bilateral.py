"""Causal bilateral L1 evidence for direct-action research.

This module is diagnostic/research-only.  It never fills missing bilateral depth
from the selected leg.  A row is executable on both YES and NO only when the
native producer serialized both prices and both bid/ask quantities from the same
causal PM cut.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from research.walk_forward_v2.core import (
    HORIZONS_MS,
    json_lines,
    native_wall_ns,
    valid_native,
)

SCHEMA = "polymarket_direct_action_bilateral_l1_v1"


def paired_l1_state(row):
    if row.get("repricing_pair_valid") is not True:
        return {"state": "UNAVAILABLE"}
    try:
        yes_bid = int(row["yes_bid_e4"]) / 10000
        yes_ask = int(row["yes_ask_e4"]) / 10000
        no_bid = int(row["no_bid_e4"]) / 10000
        no_ask = int(row["no_ask_e4"]) / 10000
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"state": "UNAVAILABLE"}
    if not (0 < yes_bid < yes_ask < 1 and 0 < no_bid < no_ask < 1):
        return {"state": "UNAVAILABLE"}

    raw_quantities = {}
    complete = True
    for key in (
        "yes_bid_quantity", "yes_ask_quantity",
        "no_bid_quantity", "no_ask_quantity",
    ):
        value = row.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raw_quantities[key] = None
            complete = False
        else:
            raw_quantities[key] = value / 1_000_000

    ready = (
        complete
        and raw_quantities["yes_bid_quantity"] > 0
        and raw_quantities["yes_ask_quantity"] > 0
        and raw_quantities["no_bid_quantity"] > 0
        and raw_quantities["no_ask_quantity"] > 0
    )
    return {
        "state": "BILATERAL_EXECUTABLE_READY" if ready else "PRICES_ONLY",
        "yes": {
            "bid": yes_bid,
            "ask": yes_ask,
            "bid_quantity": raw_quantities["yes_bid_quantity"],
            "ask_quantity": raw_quantities["yes_ask_quantity"],
        },
        "no": {
            "bid": no_bid,
            "ask": no_ask,
            "bid_quantity": raw_quantities["no_bid_quantity"],
            "ask_quantity": raw_quantities["no_ask_quantity"],
        },
    }


def _origin_key(row):
    try:
        version = int(row.get("repricing_origin_signal_version") or 0)
        decision = int(row.get("decision_monotonic_ns") or 0)
        if version <= 0 or decision <= 0:
            return None
        return (
            str(row["server_id"]),
            str(row["run_id"]),
            str(row["capture_id"]),
            str(row["market_id"]),
            version,
            decision,
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _valid_origin(row):
    return (
        valid_native(row)
        and row.get("kind") == 2
        and row.get("signal_valid") is True
        and row.get("confirmed_non_opposing") is True
        and row.get("book_valid") is True
        and _origin_key(row) is not None
        and native_wall_ns(row) > 0
    )


def _valid_label(row):
    if not valid_native(row) or row.get("kind") != 6:
        return False
    if _origin_key(row) is None or row.get("repricing_pair_valid") is not True:
        return False
    try:
        horizon = int(row.get("repricing_horizon_ms") or 0)
        decision = int(row.get("decision_monotonic_ns") or 0)
        observed = int(row.get("observed_monotonic_ns") or 0)
        close = int(row.get("close_monotonic_ns") or 0)
        return (
            horizon in HORIZONS_MS
            and decision > 0
            and observed >= decision + horizon * 1_000_000
            and close > decision + horizon * 1_000_000
            and native_wall_ns(row) > 0
        )
    except (TypeError, ValueError, OverflowError):
        return False


def build_bilateral_evidence(paths):
    """Return only origins whose decision and future PM cuts are bilaterally executable."""
    origins = {}
    labels = defaultdict(dict)
    counts = Counter()
    by_asset = defaultdict(Counter)

    for path in paths:
        for row in json_lines(path):
            if row is None or row.get("schema") != "polymarket_v7_native_observation_v1":
                continue
            kind = row.get("kind")
            if kind == 2:
                if not _valid_origin(row):
                    counts["decision_invalid_or_unmatched"] += 1
                    continue
                key = _origin_key(row)
                state = paired_l1_state(row)
                counts["decision_" + state["state"]] += 1
                by_asset[str(row.get("asset") or "UNKNOWN")]["decision_" + state["state"]] += 1
                if key in origins:
                    # Repeated control observations for the same producer origin
                    # are not silently collapsed when economics differ.
                    counts["duplicate_origin_key"] += 1
                    continue
                origins[key] = {
                    "key": key,
                    "asset": str(row.get("asset") or "UNKNOWN"),
                    "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
                    "decision_wall_ns": native_wall_ns(row),
                    "direction": int(row.get("direction") or 0),
                    "signal_age_ns": int(row.get("signal_age_ns") or 0),
                    "tte_ns": int(row.get("tte_ns") or 0),
                    "minimum": float(row.get("minimum_order_microunits") or 0) / 1_000_000,
                    "fee_rate": float(row.get("fee_rate") or 0.0),
                    "fee_exponent": float(row.get("fee_exponent") or 1.0),
                    "paired_l1": state,
                }
            elif kind == 6:
                if not _valid_label(row):
                    counts["label_invalid_or_censored"] += 1
                    continue
                key = _origin_key(row)
                horizon = int(row["repricing_horizon_ms"])
                state = paired_l1_state(row)
                counts["label_" + state["state"]] += 1
                by_asset[str(row.get("asset") or "UNKNOWN")]["label_" + state["state"]] += 1
                prior = labels[key].get(horizon)
                value = {
                    "horizon_ms": horizon,
                    "information_wall_ns": native_wall_ns(row),
                    "paired_l1": state,
                }
                if prior is not None and prior != value:
                    raise ValueError("CONFLICTING_BILATERAL_LABEL")
                labels[key][horizon] = value

    records = []
    for key, origin in sorted(
        origins.items(), key=lambda item: (item[1]["decision_wall_ns"], item[0])
    ):
        if origin["paired_l1"]["state"] != "BILATERAL_EXECUTABLE_READY":
            continue
        future = {
            str(h): value
            for h, value in sorted(labels.get(key, {}).items())
            if value["paired_l1"]["state"] == "BILATERAL_EXECUTABLE_READY"
        }
        if not future:
            continue
        records.append({**origin, "future": future})

    summary = {
        "schema": SCHEMA + "_summary",
        "origins": len(origins),
        "origins_bilateral_ready": sum(
            value["paired_l1"]["state"] == "BILATERAL_EXECUTABLE_READY"
            for value in origins.values()
        ),
        "records_with_bilateral_future": len(records),
        "counts": dict(counts),
        "by_asset": {asset: dict(values) for asset, values in sorted(by_asset.items())},
        "counterfactual_side_executable": bool(records),
        "missing_depth_is_never_imputed": True,
    }
    return records, summary
