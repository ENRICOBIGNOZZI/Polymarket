"""Rich-history direct net-PnL learning for Polymarket PAPER research.

Purpose:
- use substantially more causal history than the 2H screen;
- evaluate every result pooled and separately by crypto;
- expand exit-horizon support and fail closed when producer labels are absent;
- measure marginal value of nested information sets;
- learn E[net cash PnL | X, asset, action] with small L2 ridge regularization;
- keep tau=0 as the primary maximum-frequency economically coherent entry rule.

Research only. No execution, deployment, authentication, promotion or real capital.
Missing/censored evidence remains missing and is never converted to zero PnL.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics

import numpy as np

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, finite
from research.walk_forward_v3.direct_action import (
    _valid_state, decision_action_sides, selected_action_side,
)
from research.walk_forward_v3 import multi_alpha_2h as base

SCHEMA = "polymarket_v7_rich_history_information_v1"
LATENCIES = (5, 10, 25, 50, 100, 250, 500, 750, 1000)
EXIT_GRID_MS = (
    25, 50, 100, 250, 500, 750, 1000, 1250, 1500, 1750, 2000,
    3000, 4000, 5000, 7500, 10000,
)
FUTURE_LONG_EXIT_GRID_MS = (12500, 15000, 20000, 30000, 45000, 60000, 90000)
L2_GRID = (1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0, 8.0)
TRAIN_ACTIONS_PER_ROW = 6
MIN_EVALUABLE_FILLS = 50
MIN_PREFERRED_FILLS = 100
SIZE = 5.0

# The imported research helpers look these values up as module globals at call time.
base.LATENCIES = LATENCIES
base.EXITS = EXIT_GRID_MS

_BASE_DESIGN = base.design_record

SEMANTIC_TOKENS = (
    "signal_return", "return_250ms", "return_1s", "return_5s",
    "binance_return_100ms", "coinbase_return_100ms",
    "dispersion", "native_vol_fast", "native_vol_slow",
    "selected_spread", "selected_depth", "selected_ask",
    "signal_age", "tte_s", "imbalance",
)
PAIR_INTERACTIONS = (
    ("signal_return", "dispersion"),
    ("signal_return", "native_vol_fast"),
    ("signal_return", "selected_spread"),
    ("signal_return", "selected_depth"),
    ("signal_return", "signal_age"),
    ("signal_return", "tte_s"),
    ("return_250ms", "return_1s"),
    ("return_1s", "return_5s"),
    ("dispersion", "native_vol_fast"),
    ("dispersion", "selected_spread"),
    ("native_vol_fast", "selected_spread"),
    ("selected_ask", "selected_spread"),
)
ASSET_INTERACTION_TOKENS = (
    "signal_return", "dispersion", "native_vol_fast",
    "selected_spread", "selected_depth", "return_250ms",
)


def chronological_split(rows):
    ordered = sorted(rows, key=lambda r: (int(r["decision_ns"]), str(r["decision_id"])))
    n = len(ordered)
    if n < 20:
        raise ValueError("INSUFFICIENT_HISTORY_ROWS")
    a = max(1, int(n * .60))
    b = max(a + 1, int(n * .80))
    b = min(b, n - 1)
    return {
        "TRAIN": ordered[:a],
        "VALIDATION": ordered[a:b],
        "TEST": ordered[b:],
    }


def history_window(rows, maximum_hours):
    ordered = sorted(rows, key=lambda r: (int(r["decision_ns"]), str(r["decision_id"])))
    if not ordered:
        raise ValueError("NO_VALID_CAUSAL_DECISIONS")
    end_ns = int(ordered[-1]["decision_ns"]) + 1
    available_start = int(ordered[0]["decision_ns"])
    requested_start = end_ns - int(float(maximum_hours) * 3600 * 1e9)
    start_ns = max(available_start, requested_start)
    selected = [r for r in ordered if int(r["decision_ns"]) >= start_ns]
    if len(selected) < 20:
        raise ValueError("INSUFFICIENT_SELECTED_HISTORY")
    assets = sorted({str(r.get("asset") or "UNKNOWN") for r in selected})
    return {
        "start_ns": start_ns,
        "end_ns": end_ns,
        "hours": (end_ns - start_ns) / 3.6e12,
        "rows": len(selected),
        "assets": assets,
        "contracts": sorted({str(r.get("horizon") or "UNKNOWN") for r in selected}),
        "markets": len({str(r["market_id"]) for r in selected}),
        "selection_policy": "MAXIMUM_RECENT_CAUSAL_HISTORY_BOUNDED_BY_REQUEST_NO_PNL",
        "profitability_used_for_selection": False,
    }, selected


def _semantic_values(record):
    out = {}
    for token in SEMANTIC_TOKENS:
        matches = [
            (k, float(v)) for k, v in record.items()
            if finite(v) and token in k.lower()
        ]
        if matches:
            # Deterministic preference for shortest/canonical-looking name.
            matches.sort(key=lambda kv: (len(kv[0]), kv[0]))
            out[token] = matches[0][1]
    return out


def rich_design_record(row, side, latency, horizon, info_keys, tape_index=None, delay_ms=0):
    rec = _BASE_DESIGN(row, side, latency, horizon, info_keys, tape_index, delay_ms)
    if rec is None:
        return None
    semantic = _semantic_values(rec)

    # Controlled nonlinear basis: enough flexibility to learn regimes without
    # creating an unbounded pairwise feature explosion.
    for token, value in semantic.items():
        rec["nl.abs::" + token] = abs(value)
        rec["nl.slog::" + token] = math.copysign(math.log1p(abs(value)), value)
        if token in {
            "signal_return", "return_250ms", "return_1s", "return_5s",
            "dispersion", "native_vol_fast", "selected_spread", "imbalance",
        }:
            rec["nl.square::" + token] = value * value

    for left, right in PAIR_INTERACTIONS:
        if left in semantic and right in semantic:
            rec[f"ix::{left}*{right}"] = semantic[left] * semantic[right]

    log_exit = float(rec.get("action.log_exit") or 0.0)
    log_latency = float(rec.get("system.log_latency") or 0.0)
    side_sign = float(rec.get("action.side_sign") or 0.0)
    for token in (
        "signal_return", "dispersion", "native_vol_fast",
        "selected_spread", "return_250ms", "return_1s",
    ):
        if token in semantic:
            value = semantic[token]
            rec[f"ix::{token}*log_exit"] = value * log_exit
            rec[f"ix::{token}*log_latency"] = value * log_latency
            rec[f"ix::{token}*side"] = value * side_sign

    asset_keys = [k for k, v in rec.items() if k.startswith("asset::") and finite(v) and float(v) != 0]
    for asset_key in asset_keys:
        asset = asset_key.split("::", 1)[1]
        for token in ASSET_INTERACTION_TOKENS:
            if token in semantic:
                rec[f"asset_ix::{asset}::{token}"] = semantic[token]

    contract_keys = [k for k, v in rec.items() if k.startswith("contract::") and finite(v) and float(v) != 0]
    for contract_key in contract_keys:
        contract = contract_key.split("::", 1)[1]
        for token in ("signal_return", "dispersion", "selected_spread"):
            if token in semantic:
                rec[f"contract_ix::{contract}::{token}"] = semantic[token]
    return rec


# Patch only this imported research module in this process. No runtime/live code changes.
base.design_record = rich_design_record


def deterministic_cells(decision_id, count=TRAIN_ACTIONS_PER_ROW):
    cells = [(l, h) for l in LATENCIES for h in EXIT_GRID_MS if h > l]
    if count >= len(cells):
        return cells
    raw = hashlib.sha256(str(decision_id).encode("utf-8")).digest()
    start = int.from_bytes(raw[:4], "big") % len(cells)
    # 37 is coprime to the current dense action grid.
    stride = 37
    picked = []
    seen = set()
    i = start
    while len(picked) < count:
        if i not in seen:
            seen.add(i)
            picked.append(cells[i])
        i = (i + stride) % len(cells)
    return picked


def compact_economics(row, side, latency, horizon, cache):
    key = (str(row["decision_id"]), str(side), int(latency), int(horizon))
    prior = cache.get(key)
    if prior is not None:
        return prior
    economics, state = base.native_execute_side_cell(row, latency, horizon, side)
    if economics is None:
        value = (None, 0.0, str(state))
    else:
        value = (
            float(economics.get("cash_pnl") or 0.0),
            float(economics.get("filled") or 0.0),
            str(state),
        )
    cache[key] = value
    return value


def sampled_examples(rows, info_keys, tape_index, economics_cache):
    records, targets = [], []
    states = Counter()
    for row in rows:
        for latency, horizon in deterministic_cells(row["decision_id"]):
            for side in decision_action_sides(row):
                rec = rich_design_record(row, side, latency, horizon, info_keys, tape_index, 0)
                if rec is None:
                    continue
                cash, filled, state = compact_economics(
                    row, side, latency, horizon, economics_cache)
                states[state] += 1
                if cash is None:
                    continue
                records.append(rec)
                targets.append(float(cash))
    return records, targets, dict(states)


def mse(model, records, targets):
    if not records:
        return None
    pred = model.predict(records)
    y = np.asarray(targets, dtype=float)
    if len(pred) != len(y) or not len(y):
        return None
    return float(np.mean((pred - y) ** 2))


def fit_ridge_validation(train_rows, validation_rows, info_keys, tape_index, economics_cache):
    train_x, train_y, train_states = sampled_examples(
        train_rows, info_keys, tape_index, economics_cache)
    val_x, val_y, val_states = sampled_examples(
        validation_rows, info_keys, tape_index, economics_cache)
    if len(train_x) < 100 or len(val_x) < 20:
        raise ValueError("INSUFFICIENT_RIDGE_EXAMPLES")
    candidates = []
    for lam in L2_GRID:
        try:
            model = base.Ridge(lam).fit(train_x, train_y)
            score = mse(model, val_x, val_y)
        except (ValueError, np.linalg.LinAlgError):
            continue
        if score is not None and finite(score):
            candidates.append((float(score), float(lam), model))
    if not candidates:
        raise ValueError("NO_VALID_RIDGE_LAMBDA")
    candidates.sort(key=lambda x: (x[0], x[1]))
    best_mse, best_lambda, best_model = candidates[0]
    return best_model, {
        "selection_objective": "MIN_VALIDATION_MSE_ON_OBSERVED_NET_CASH_PNL",
        "pnl_used_for_lambda_selection": False,
        "train_examples": len(train_x),
        "validation_examples": len(val_x),
        "train_state_counts": train_states,
        "validation_state_counts": val_states,
        "lambda_grid": list(L2_GRID),
        "validation_mse_by_lambda": {
            str(lam): score for score, lam, _ in candidates
        },
        "selected_lambda": best_lambda,
        "selected_validation_mse": best_mse,
        "model_dimension": int(best_model.dimension),
    }


def evidence_band(fills):
    fills = int(fills)
    if fills >= MIN_PREFERRED_FILLS:
        return "PREFERRED_100_PLUS"
    if fills >= MIN_EVALUABLE_FILLS:
        return "EVALUABLE_50_99"
    if fills > 0:
        return "EXPLORATORY_LT50"
    return "NO_FILLS"


def stats_from_events(events, observed):
    stats = base.equity_stats(events)
    return {
        "observed_actions": int(observed),
        "fills": int(stats["fills"]),
        "total_pnl": float(stats["total_pnl"]) if observed else None,
        "pnl_per_fill": stats["pnl_per_fill"] if observed else None,
        "hit_rate": stats["hit_rate"] if observed else None,
        "max_drawdown": stats["max_drawdown"] if observed else None,
        "evidence_band": evidence_band(stats["fills"]),
    }


def evaluate_policy(model, rows, info_keys, tape_index, economics_cache, tau=0.0):
    pooled = {}
    by_asset = {}
    events_by_cell = {}
    for latency in LATENCIES:
        for horizon in EXIT_GRID_MS:
            if horizon <= latency:
                continue
            key = f"{latency}::{horizon}"
            observed = 0
            censored = Counter()
            no_trade = 0
            trades = 0
            events = []
            asset_events = defaultdict(list)
            asset_observed = Counter()
            asset_trades = Counter()
            asset_no_trade = Counter()
            asset_censored = defaultdict(Counter)
            for row in rows:
                asset = str(row.get("asset") or "UNKNOWN")
                candidates, sides = [], []
                for side in decision_action_sides(row):
                    rec = rich_design_record(
                        row, side, latency, horizon, info_keys, tape_index, 0)
                    if rec is not None:
                        candidates.append(rec)
                        sides.append(side)
                if not candidates:
                    censored["NO_SIDE_FEATURE_STATE"] += 1
                    asset_censored[asset]["NO_SIDE_FEATURE_STATE"] += 1
                    continue
                pred = model.predict(candidates)
                best = int(np.argmax(pred))
                edge = float(pred[best])
                if not finite(edge) or edge <= float(tau):
                    no_trade += 1
                    asset_no_trade[asset] += 1
                    continue
                side = sides[best]
                cash, filled, state = compact_economics(
                    row, side, latency, horizon, economics_cache)
                if cash is None:
                    censored[state] += 1
                    asset_censored[asset][state] += 1
                    continue
                observed += 1
                trades += 1
                asset_observed[asset] += 1
                asset_trades[asset] += 1
                if filled > 0:
                    event = {
                        "decision_ns": int(row["decision_ns"]),
                        "decision_id": str(row["decision_id"]),
                        "asset": asset,
                        "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
                        "cash_pnl": float(cash),
                        "prediction": edge,
                    }
                    events.append(event)
                    asset_events[asset].append(event)
            cell = stats_from_events(events, observed)
            cell.update({
                "opportunities": len(rows),
                "trade_count": int(trades),
                "no_trade_count": int(no_trade),
                "censored": int(sum(censored.values())),
                "censoring_reasons": dict(censored),
                "tau": float(tau),
            })
            pooled[key] = cell
            events_by_cell[key] = sorted(
                events, key=lambda e: (e["decision_ns"], e["decision_id"]))
            assets = sorted({
                str(r.get("asset") or "UNKNOWN") for r in rows
            })
            by_asset[key] = {}
            for asset in assets:
                astats = stats_from_events(
                    asset_events.get(asset, []), asset_observed.get(asset, 0))
                astats.update({
                    "trade_count": int(asset_trades.get(asset, 0)),
                    "no_trade_count": int(asset_no_trade.get(asset, 0)),
                    "censored": int(sum(asset_censored[asset].values())),
                    "censoring_reasons": dict(asset_censored[asset]),
                })
                by_asset[key][asset] = astats
    return {
        "metrics": pooled,
        "by_asset": by_asset,
        "events": events_by_cell,
    }


def evaluate_baseline(rows, economics_cache):
    pooled = {}
    by_asset = {}
    events_by_cell = {}
    for latency in LATENCIES:
        for horizon in EXIT_GRID_MS:
            if horizon <= latency:
                continue
            key = f"{latency}::{horizon}"
            observed = 0
            censored = Counter()
            events = []
            asset_events = defaultdict(list)
            asset_observed = Counter()
            asset_censored = defaultdict(Counter)
            for row in rows:
                asset = str(row.get("asset") or "UNKNOWN")
                side = selected_action_side(row)
                cash, filled, state = compact_economics(
                    row, side, latency, horizon, economics_cache)
                if cash is None:
                    censored[state] += 1
                    asset_censored[asset][state] += 1
                    continue
                observed += 1
                asset_observed[asset] += 1
                if filled > 0:
                    event = {
                        "decision_ns": int(row["decision_ns"]),
                        "decision_id": str(row["decision_id"]),
                        "asset": asset,
                        "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
                        "cash_pnl": float(cash),
                    }
                    events.append(event)
                    asset_events[asset].append(event)
            cell = stats_from_events(events, observed)
            cell.update({
                "opportunities": len(rows),
                "censored": int(sum(censored.values())),
                "censoring_reasons": dict(censored),
            })
            pooled[key] = cell
            events_by_cell[key] = sorted(
                events, key=lambda e: (e["decision_ns"], e["decision_id"]))
            assets = sorted({str(r.get("asset") or "UNKNOWN") for r in rows})
            by_asset[key] = {}
            for asset in assets:
                astats = stats_from_events(
                    asset_events.get(asset, []), asset_observed.get(asset, 0))
                astats.update({
                    "censored": int(sum(asset_censored[asset].values())),
                    "censoring_reasons": dict(asset_censored[asset]),
                })
                by_asset[key][asset] = astats
    return {"metrics": pooled, "by_asset": by_asset, "events": events_by_cell}


def delta_summary(current, previous):
    pooled = {}
    by_asset = {}
    for cell, value in current["metrics"].items():
        prior = previous["metrics"].get(cell) or {}
        a, b = value.get("total_pnl"), prior.get("total_pnl")
        pooled[cell] = None if not (finite(a) and finite(b)) else float(a) - float(b)
    for cell, assets in current["by_asset"].items():
        by_asset[cell] = {}
        for asset, value in assets.items():
            prior = (previous["by_asset"].get(cell) or {}).get(asset) or {}
            a, b = value.get("total_pnl"), prior.get("total_pnl")
            by_asset[cell][asset] = (
                None if not (finite(a) and finite(b)) else float(a) - float(b)
            )
    values = [v for v in pooled.values() if finite(v)]
    return {
        "pooled": pooled,
        "by_asset": by_asset,
        "pooled_score": {
            "comparable_cells": len(values),
            "positive_cells": sum(float(v) > 0 for v in values),
            "positive_fraction": (
                sum(float(v) > 0 for v in values) / len(values)
                if values else None
            ),
            "median_delta_pnl": statistics.median(values) if values else None,
            "mean_delta_pnl": statistics.fmean(values) if values else None,
        },
    }


def write_grids(output, baseline, models):
    pooled_path = output / "33_rich_pooled_grid.csv"
    fields = [
        "model", "entry_latency_ms", "exit_horizon_ms", "total_pnl",
        "observed_actions", "fills", "pnl_per_fill", "hit_rate",
        "max_drawdown", "evidence_band",
    ]
    with pooled_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        all_models = {"BASELINE_SELECTED_SIDE": baseline, **models}
        for name, result in all_models.items():
            for cell, m in result["metrics"].items():
                latency, horizon = map(int, cell.split("::"))
                writer.writerow({
                    "model": name,
                    "entry_latency_ms": latency,
                    "exit_horizon_ms": horizon,
                    **{k: m.get(k) for k in fields[3:]},
                })

    crypto_path = output / "34_rich_crypto_grid.csv"
    fields2 = [
        "model", "asset", "entry_latency_ms", "exit_horizon_ms",
        "total_pnl", "observed_actions", "fills", "pnl_per_fill",
        "hit_rate", "max_drawdown", "evidence_band",
    ]
    with crypto_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields2, lineterminator="\n")
        writer.writeheader()
        all_models = {"BASELINE_SELECTED_SIDE": baseline, **models}
        for name, result in all_models.items():
            for cell, assets in result["by_asset"].items():
                latency, horizon = map(int, cell.split("::"))
                for asset, m in sorted(assets.items()):
                    writer.writerow({
                        "model": name,
                        "asset": asset,
                        "entry_latency_ms": latency,
                        "exit_horizon_ms": horizon,
                        **{k: m.get(k) for k in fields2[4:]},
                    })
    return pooled_path, crypto_path


def write_equity(output, baseline, models, start_ns, end_ns):
    path = output / "37_rich_equity_1m.csv.gz"
    points = int(math.ceil((end_ns - start_ns) / 60e9)) + 1
    with gzip.open(path, "wt", newline="", encoding="utf-8") as handle:
        fields = ["model", "asset", "cell", "minute", "cumulative_pnl"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        all_models = {"BASELINE_SELECTED_SIDE": baseline, **models}
        for name, result in all_models.items():
            for cell, events in result["events"].items():
                groups = {"ALL": events}
                assets = sorted({e["asset"] for e in events})
                for asset in assets:
                    groups[asset] = [e for e in events if e["asset"] == asset]
                for asset, seq in groups.items():
                    seq = sorted(seq, key=lambda e: (e["decision_ns"], e["decision_id"]))
                    pos = 0
                    total = 0.0
                    for minute in range(points):
                        cutoff = start_ns + minute * 60_000_000_000
                        while pos < len(seq) and int(seq[pos]["decision_ns"]) <= cutoff:
                            total += float(seq[pos]["cash_pnl"])
                            pos += 1
                        writer.writerow({
                            "model": name,
                            "asset": asset,
                            "cell": cell,
                            "minute": minute,
                            "cumulative_pnl": total,
                        })
    return path


def fill_adequacy(models):
    out = {}
    for name, result in models.items():
        per_asset = defaultdict(lambda: {
            "cells_with_fills": 0,
            "cells_ge_50": 0,
            "cells_ge_100": 0,
            "max_fills": 0,
        })
        for assets in result["by_asset"].values():
            for asset, m in assets.items():
                fills = int(m.get("fills") or 0)
                if fills > 0:
                    per_asset[asset]["cells_with_fills"] += 1
                if fills >= 50:
                    per_asset[asset]["cells_ge_50"] += 1
                if fills >= 100:
                    per_asset[asset]["cells_ge_100"] += 1
                per_asset[asset]["max_fills"] = max(
                    per_asset[asset]["max_fills"], fills)
        out[name] = dict(sorted(per_asset.items()))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--minimum-wall-ns", type=int, required=True)
    p.add_argument("--maximum-history-hours", type=float, default=168.0)
    a = p.parse_args(argv)

    a.output_dir.mkdir(parents=True, exist_ok=True)
    data = build_dataset(
        a.root,
        minimum_wall_ns=a.minimum_wall_ns,
        include_settlement_labels=False,
        use_compact_window_index=True,
    )
    if data.get("input_state") != "READY":
        raise ValueError("DATA_NOT_READY:" + str(data.get("input_state")))

    rows = [r for r in data["decisions"] if _valid_state(r)]
    window, rows = history_window(rows, a.maximum_history_hours)
    splits = chronological_split(rows)
    start_ns, end_ns = int(window["start_ns"]), int(window["end_ns"])

    tape_index, feature_diag = base.load_feature_tape(a.root, start_ns, end_ns)
    nested, available = base.nested_family_keys(rows, tape_index)
    families = []
    previous = set()
    for name, _patterns in base.FAMILIES:
        keys = tuple(nested.get(name) or ())
        added = set(keys) - previous
        previous = set(keys)
        if name in ("F0_PM_ONLY", "F1_BASELINE") or added:
            families.append((name, keys, sorted(added)))
    all_keys = tuple(sorted(set(available)))
    if not families or set(all_keys) != set(families[-1][1]):
        families.append(("F12_ALL_AVAILABLE", all_keys, sorted(set(all_keys) - previous)))
    elif families[-1][0] != "F12_ALL_AVAILABLE":
        families.append(("F12_ALL_AVAILABLE", all_keys, []))

    economics_cache = {}
    baseline = evaluate_baseline(splits["TEST"], economics_cache)
    model_results = {}
    model_receipts = {}
    incremental = {}
    previous_result = baseline

    for family, keys, added in families:
        if family not in ("F0_PM_ONLY", "F1_BASELINE", "F12_ALL_AVAILABLE") and not added:
            continue
        try:
            model, receipt = fit_ridge_validation(
                splits["TRAIN"], splits["VALIDATION"], keys, tape_index,
                economics_cache)
        except (ValueError, np.linalg.LinAlgError) as exc:
            model_receipts[family] = {
                "state": "INSUFFICIENT_DATA",
                "reason": str(exc),
                "features": list(keys),
                "features_added": list(added),
            }
            continue
        result = evaluate_policy(
            model, splits["TEST"], keys, tape_index, economics_cache, tau=0.0)
        model_results[family] = result
        receipt.update({
            "state": "READY",
            "features": list(keys),
            "features_added": list(added),
            "tau": 0.0,
            "target": "OBSERVED_EXECUTABLE_NET_CASH_PNL",
            "entry_rule": "E_NET_PNL_GIVEN_X_ASSET_ACTION_GT_TAU",
            "rich_basis": True,
        })
        model_receipts[family] = receipt
        incremental[family] = delta_summary(result, previous_result)
        previous_result = result

    write_grids(a.output_dir, baseline, model_results)
    write_equity(
        a.output_dir, baseline, model_results, start_ns, end_ns)

    manifest = {
        "schema": SCHEMA,
        **SAFETY,
        "research_only": True,
        "automatic_promotion": False,
        "window": window,
        "split_rows": {k: len(v) for k, v in splits.items()},
        "latencies_ms": list(LATENCIES),
        "requested_exit_horizons_ms": list(EXIT_GRID_MS),
        "future_long_exit_horizons_requiring_new_capture_ms": list(FUTURE_LONG_EXIT_GRID_MS),
        "l2_grid": list(L2_GRID),
        "primary_tau": 0.0,
        "minimum_evaluable_fills": MIN_EVALUABLE_FILLS,
        "minimum_preferred_fills": MIN_PREFERRED_FILLS,
        "target_size_shares": SIZE,
        "data_sha256": data.get("data_sha256"),
        "source_files": data.get("sources"),
        "book_evidence": data.get("book_evidence"),
        "feature_tape_diagnostics": feature_diag,
        "available_feature_keys": list(available),
        "families_attempted": [name for name, _, _ in families],
        "models_ready": sorted(model_results),
        "economics_cache_entries": len(economics_cache),
        "selection_guards": {
            "history_selected_on_pnl": False,
            "lambda_selected_on_test_pnl": False,
            "lambda_objective": "VALIDATION_MSE",
            "tau_tuned_on_test": False,
            "missing_as_zero": False,
        },
    }
    atomic_json(a.output_dir / "30_rich_history_manifest.json", manifest)
    atomic_json(a.output_dir / "31_rich_data_coverage_by_crypto.json", {
        "schema": SCHEMA + "_coverage_v1",
        **SAFETY,
        "rows_by_asset": dict(Counter(str(r.get("asset") or "UNKNOWN") for r in rows)),
        "rows_by_asset_contract": {
            f"{asset}::{horizon}": count
            for (asset, horizon), count in sorted(Counter(
                (str(r.get("asset") or "UNKNOWN"), str(r.get("horizon") or "UNKNOWN"))
                for r in rows
            ).items())
        },
        "split_rows_by_asset": {
            split: dict(Counter(str(r.get("asset") or "UNKNOWN") for r in subset))
            for split, subset in splits.items()
        },
        "requested_exit_horizons_ms": list(EXIT_GRID_MS),
        "future_long_exit_horizons_requiring_new_capture_ms": list(FUTURE_LONG_EXIT_GRID_MS),
        "target_support_by_horizon": {
            str(h): sum(
                (r.get("targets") or {}).get(str(h), {}).get("state") == "OBSERVED"
                for r in rows
            )
            for h in EXIT_GRID_MS
        },
        "arrival_support_by_latency": {
            str(l): sum(
                isinstance((r.get("arrivals") or {}).get(str(l)), dict)
                for r in rows
            )
            for l in LATENCIES
        },
        "target_support_by_asset_horizon": {
            asset: {
                str(h): sum(
                    str(r.get("asset") or "UNKNOWN") == asset
                    and (r.get("targets") or {}).get(str(h), {}).get("state") == "OBSERVED"
                    for r in rows
                )
                for h in EXIT_GRID_MS
            }
            for asset in sorted({str(r.get("asset") or "UNKNOWN") for r in rows})
        },
        "book_evidence": data.get("book_evidence"),
    })
    atomic_json(a.output_dir / "32_rich_information_models.json", {
        "schema": SCHEMA + "_models_v1",
        **SAFETY,
        "model_receipts": model_receipts,
        "baseline": {
            "metrics": baseline["metrics"],
            "by_asset": baseline["by_asset"],
        },
        "models": {
            name: {
                "metrics": result["metrics"],
                "by_asset": result["by_asset"],
            }
            for name, result in model_results.items()
        },
    })
    atomic_json(a.output_dir / "35_rich_incremental_information.json", {
        "schema": SCHEMA + "_incremental_v1",
        **SAFETY,
        "definition": "PnL(F_k)-PnL(F_{k-1}) on identical TEST cell and asset",
        "incremental": incremental,
    })
    atomic_json(a.output_dir / "36_rich_fill_adequacy.json", {
        "schema": SCHEMA + "_fill_adequacy_v1",
        **SAFETY,
        "minimum_evaluable_fills": MIN_EVALUABLE_FILLS,
        "minimum_preferred_fills": MIN_PREFERRED_FILLS,
        "baseline": fill_adequacy({"BASELINE_SELECTED_SIDE": baseline}),
        "models": fill_adequacy(model_results),
    })

    print(json.dumps({
        "state": "READY",
        "rows": len(rows),
        "assets": window["assets"],
        "hours": window["hours"],
        "models_ready": sorted(model_results),
        "requested_exits": list(EXIT_GRID_MS),
        "cache_entries": len(economics_cache),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
