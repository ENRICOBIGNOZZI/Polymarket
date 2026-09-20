#!/usr/bin/env python3
"""Build causal slow/fast probability research rows from the frozen order audit.

This is a data-contract tool only. It does not fit coefficients, impute missing
slow fields, authorize execution, or claim that slow context improves PnL.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_slow_fast_probability_dataset_v1"
SLOW_FIELDS = (
    "spot_composite", "volatility_medium", "volatility_slow", "return_5s",
    "dispersion_bps", "binance_funding", "binance_open_interest",
    "bybit_funding", "bybit_open_interest", "deribit_funding",
    "deribit_open_interest", "oracle_value", "opening_reference",
)
SLOW_MASK = (1 << len(SLOW_FIELDS)) - 1


class SlowFastDatasetError(ValueError):
    pass


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SlowFastDatasetError(name)
    out = float(value)
    if not math.isfinite(out):
        raise SlowFastDatasetError(name)
    return out


def _slow_cut(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "fresh_mask": 0,
            "model_used_mask": 0,
            "decision_monotonic_ns": None,
            "max_input_receive_monotonic_ns": None,
            "values": {name: None for name in SLOW_FIELDS},
            "source_receive_monotonic_ns": {name: None for name in SLOW_FIELDS},
        }
    if not isinstance(value, dict) or value.get("schema") != "polymarket_v7_slow_context_cut_v1":
        raise SlowFastDatasetError("slow_context_schema")
    fresh = value.get("fresh_mask")
    decision = value.get("decision_monotonic_ns")
    max_input = value.get("max_input_receive_monotonic_ns")
    used = value.get("model_used_mask", 0)
    fields = value.get("fields")
    if type(fresh) is not int or fresh < 0 or fresh & ~SLOW_MASK:
        raise SlowFastDatasetError("slow_fresh_mask")
    if type(used) is not int or used < 0 or used & ~SLOW_MASK:
        raise SlowFastDatasetError("slow_model_used_mask")
    if type(decision) is not int or decision <= 0:
        raise SlowFastDatasetError("slow_decision_clock")
    if type(max_input) is not int or max_input < 0 or max_input > decision:
        raise SlowFastDatasetError("slow_max_input_clock")
    if not isinstance(fields, dict):
        raise SlowFastDatasetError("slow_fields")
    values: dict[str, float | None] = {}
    receives: dict[str, int | None] = {}
    observed_max = 0
    for index, name in enumerate(SLOW_FIELDS):
        node = fields.get(name)
        is_fresh = bool(fresh & (1 << index))
        if not is_fresh:
            values[name] = None
            receives[name] = None
            continue
        if not isinstance(node, dict):
            raise SlowFastDatasetError(f"slow_missing_fresh_{name}")
        val = _finite(node.get("value"), f"slow_value_{name}")
        receive = node.get("receive_monotonic_ns")
        expires = node.get("expires_monotonic_ns")
        version = node.get("source_version")
        if (type(receive) is not int or type(expires) is not int or type(version) is not int
                or receive <= 0 or receive > decision or expires < decision or version <= 0):
            raise SlowFastDatasetError(f"slow_clock_{name}")
        values[name] = val
        receives[name] = receive
        observed_max = max(observed_max, receive)
    if observed_max != max_input:
        raise SlowFastDatasetError("slow_max_input_mismatch")
    return {
        "fresh_mask": fresh,
        "model_used_mask": used,
        "decision_monotonic_ns": decision,
        "max_input_receive_monotonic_ns": max_input,
        "values": values,
        "source_receive_monotonic_ns": receives,
    }


def build(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    contexts: Counter[str] = Counter()
    for row in rows:
        if row.get("feature_join") != "UNIQUE":
            excluded["FEATURE_JOIN_NOT_UNIQUE"] += 1
            continue
        if row.get("selected_outcome") not in (0.0, 1.0):
            excluded["OUTCOME_MISSING"] += 1
            continue
        features = row.get("features")
        if not isinstance(features, dict):
            excluded["FAST_FEATURES_MISSING"] += 1
            continue
        try:
            slow = _slow_cut(row.get("slow_context"))
        except SlowFastDatasetError:
            excluded["INVALID_SLOW_CAUSAL_CUT"] += 1
            continue
        asset, horizon = row.get("asset"), row.get("horizon")
        if not isinstance(asset, str) or not isinstance(horizon, str):
            excluded["CONTEXT_MISSING"] += 1
            continue
        context = f"{asset}:{horizon}"
        contexts[context] += 1
        for name, value in slow["values"].items():
            if value is not None:
                coverage[name] += 1
        out.append({
            "schema": SCHEMA,
            "order_id": row.get("order_id"),
            "market_id": row.get("market_id"),
            "run_id": row.get("run_id"),
            "asset": asset,
            "horizon": horizon,
            "decision_ts_ms": row.get("decision_ts_ms"),
            "close_ts_ms": row.get("close_ts_ms"),
            "selected_outcome": row["selected_outcome"],
            "fast_features": features,
            "external_features": row.get("external_features"),
            "slow_context": slow,
            "slow_context_observational_only": slow["model_used_mask"] == 0,
        })
    n = len(out)
    summary = {
        "schema": "polymarket_v7_slow_fast_probability_dataset_summary_v1",
        "rows": n,
        "excluded": dict(excluded),
        "contexts": dict(sorted(contexts.items())),
        "slow_field_coverage": {
            name: {"count": coverage[name], "fraction": coverage[name] / n if n else None}
            for name in SLOW_FIELDS
        },
        "no_imputation": True,
        "execution_authority": False,
        "claim": "CAUSAL_RESEARCH_DATASET_NOT_MODEL_OR_PROFITABILITY_PROOF",
    }
    return out, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads(args.orders.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("orders must contain a JSON array")
    dataset, summary = build(rows)
    if args.output.exists() or args.summary.exists():
        raise SystemExit("refusing to overwrite research evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
                                   for row in dataset), encoding="utf-8")
    args.summary.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
