"""Fast two-hour multi-alpha research program.

Research-only. Selects a two-hour window using data quality only, reproduces
the current all-crypto compact-book baseline, then screens nested information
sets on the same causal PM opportunities and executable L1 economics.

No real execution, authentication, deployment, restart, or promotion.
Missing evidence is censored, never imputed to zero.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import statistics
import time

import numpy as np

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset, fee_per_share, finite
from research.walk_forward_v3.direct_action import (
    _valid_state, decision_action_sides, decision_side_state, selected_action_side,
)
from research.walk_forward_v3.dynamic_exit import DynamicExitValueModel, summarize_dynamic_exit
from research.walk_forward_v3.btc_compact_equity import (
    LATENCIES, EXITS, SIZE, stream_sessions, jsonl_sessions, stream_raw_sessions,
    session_for_row, pair_asof_session, side_state,
)

SCHEMA = "polymarket_v7_multi_alpha_2h_v1"
WINDOW_NS = 2 * 60 * 60 * 1_000_000_000
STEP_NS = 15 * 60 * 1_000_000_000
DELAYS_MS = (0, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000)

FAMILIES = (
    ("F0_PM_ONLY", ("research.pm_",)),
    ("F1_BASELINE", (
        "signal_return_bp", "signal_age_ns",
        "binance_return_100ms_bp", "coinbase_return_100ms_bp", "bybit_return_100ms_bp",
        "external.binance_return_100ms_bp", "external.coinbase_return_100ms_bp",
        "external.bybit_return_100ms_bp",
    )),
    ("F2_CROSS_VENUE", (
        "external.return_", "return_50ms", "return_100ms", "return_250ms",
        "return_500ms", "return_1s", "return_2s", "return_5s",
        "dispersion", "agreement", "fresh_venue", "composite_price",
    )),
    ("F3_TRADE_FLOW", ("trade_imbalance", "signed_volume", "trade_intensity", "volume_accel")),
    ("F4_OFI_BOOK", ("aggregate_ofi", ".ofi", "microprice", "book_imbalance", "depth_change", "depletion", "book_slope")),
    ("F5_PERP", ("perp", "basis", "mark_price", "index_price", "spot_minus")),
    ("F6_OI_FUNDING", ("open_interest", ".oi", "funding")),
    ("F7_LIQUIDATIONS", ("liquidation", "cascade")),
    ("F8_VOLATILITY", ("vol_", "volatility", "native_vol", "jump", "vol_of_vol")),
    ("F9_CROSS_ASSET", ("leader_features", "cross_asset", "common_factor", "residual_move")),
    ("F10_OPTIONS", ("deribit", "option", "iv_", "skew", "term_structure")),
    ("F11_SETTLEMENT", ("distance_to_reference", "reference_price", "oracle_price", "tte_seconds")),
)

def robust_json_lines(path):
    path = Path(path)
    try:
        with path.open("rb") as probe:
            magic = probe.read(2)
    except OSError:
        return
    opener = gzip.open if magic == b"\x1f\x8b" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except (OSError, UnicodeDecodeError):
        return

def flatten_numeric(value, prefix=""):
    out = {}
    if not isinstance(value, dict):
        return out
    for key, raw in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if finite(raw):
            out[name] = float(raw)
        elif isinstance(raw, dict):
            out.update(flatten_numeric(raw, name))
    return out

def feature_tape_paths(root):
    root = Path(root).resolve()
    paths = set()
    for base in (root, root.parent):
        for pattern in (
            "research/**/*feature*tape*.jsonl*",
            "paper_v7_london_archives/**/research/**/*feature*tape*.jsonl*",
        ):
            for path in base.glob(pattern):
                if path.is_file() and not path.is_symlink():
                    paths.add(path.resolve())
    return sorted(paths)

def load_feature_tape(root, start_ns, end_ns):
    index = defaultdict(list)
    counts = Counter()
    lo, hi = start_ns - 5_000_000_000, end_ns + 1_000_000_000
    paths = feature_tape_paths(root)
    for path in paths:
        for row in robust_json_lines(path):
            if row.get("schema") != "polymarket_v7_multi_crypto_feature_tape_v1":
                continue
            counts["rows_seen"] += 1
            if not (
                row.get("paper_only") is True
                and row.get("authenticated_execution") is False
                and row.get("real_order_submission") is False
                and row.get("execution_authority") is False
            ):
                counts["authority_rejected"] += 1
                continue
            try:
                available = int(row.get("available_at_ns") or 0)
                decision = int(row.get("decision_wall_ns") or 0)
                recorded = int(row.get("recorded_wall_ns") or 0)
            except (TypeError, ValueError):
                continue
            ready = max(available, decision, recorded)
            market = str(row.get("market_id") or "")
            if not market or ready <= 0 or not lo <= ready <= hi:
                continue
            features = flatten_numeric(row.get("features") or {})
            if not features:
                counts["empty_features"] += 1
                continue
            index[market].append({"ready_ns": ready, "features": features})
            counts["rows_retained"] += 1
    output = {}
    for market, rows in index.items():
        rows.sort(key=lambda r: r["ready_ns"])
        output[market] = {"rows": rows, "stamps": [r["ready_ns"] for r in rows]}
    return output, {
        "paths": [str(p) for p in paths], "market_count": len(output), **dict(counts)
    }

def feature_asof(index, market, cutoff_ns):
    value = index.get(str(market))
    if not value:
        return None
    pos = bisect_right(value["stamps"], int(cutoff_ns)) - 1
    return None if pos < 0 else value["rows"][pos]

def pm_features(row):
    pair = row.get("pair") if isinstance(row.get("pair"), dict) else {}
    yes = pair.get("yes") if isinstance(pair.get("yes"), dict) else {}
    no = pair.get("no") if isinstance(pair.get("no"), dict) else {}
    raw = {
        "research.pm_selected_bid": row.get("bid"),
        "research.pm_selected_ask": row.get("ask"),
        "research.pm_selected_depth": row.get("quantity"),
        "research.pm_tte_s": float(row.get("tte_ns") or 0) / 1e9,
        "research.pm_signal_age_ms": float(row.get("signal_age_ns") or 0) / 1e6,
        "research.pm_direction": row.get("direction"),
        "research.pm_yes_bid": yes.get("bid"), "research.pm_yes_ask": yes.get("ask"),
        "research.pm_yes_bid_depth": yes.get("bid_quantity"), "research.pm_yes_ask_depth": yes.get("ask_quantity"),
        "research.pm_no_bid": no.get("bid"), "research.pm_no_ask": no.get("ask"),
        "research.pm_no_bid_depth": no.get("bid_quantity"), "research.pm_no_ask_depth": no.get("ask_quantity"),
    }
    out = {k: float(v) for k, v in raw.items() if finite(v)}
    for prefix in ("yes", "no"):
        bid, ask = out.get(f"research.pm_{prefix}_bid"), out.get(f"research.pm_{prefix}_ask")
        if bid is not None and ask is not None:
            out[f"research.pm_{prefix}_spread"] = ask - bid
        bd = out.get(f"research.pm_{prefix}_bid_depth")
        ad = out.get(f"research.pm_{prefix}_ask_depth")
        if bd is not None and ad is not None and bd + ad > 0:
            out[f"research.pm_{prefix}_imbalance"] = (bd - ad) / (bd + ad)
    return out

def row_features(row, tape_index=None, delay_ms=0):
    # PM state belongs to the action-time state. External information is delayed
    # separately. At d>0, never leak the native decision-time external cut.
    features = pm_features(row)
    if int(delay_ms) == 0:
        features.update(dict(row.get("features") or {}))
    tape = None
    if tape_index:
        tape = feature_asof(
            tape_index, row["market_id"],
            int(row["decision_ns"]) - int(delay_ms) * 1_000_000,
        )
    if tape is not None:
        features.update(tape["features"])
        features["research.feature_age_ms"] = (
            int(row["decision_ns"]) - int(tape["ready_ns"])
        ) / 1_000_000.0
    return {str(k): float(v) for k, v in features.items() if finite(v)}

def nested_family_keys(rows, tape_index):
    available = set()
    for row in rows[:min(5000, len(rows))]:
        available.update(row_features(row, tape_index, 0))
    cumulative, out = set(), {}
    for name, patterns in FAMILIES:
        cumulative.update({
            key for key in available
            if any(pattern.lower() in key.lower() for pattern in patterns)
        })
        out[name] = tuple(sorted(cumulative))
    return out, tuple(sorted(available))

def execute_side_cell(row, session, latency_ms, exit_ms, side):
    """Current compact-equity semantics, generalized only by YES/NO side."""
    decision = decision_side_state(row, side)
    if decision is None:
        return None, "SIDE_DECISION_EVIDENCE_UNAVAILABLE"
    ask0, depth0 = float(decision["ask"]), float(decision["ask_quantity"])
    if float(row["minimum"]) > SIZE + 1e-12:
        return None, "VENUE_MINIMUM_ABOVE_TARGET_SIZE"
    if SIZE > depth0 + 1e-12 or SIZE * ask0 > 100.0 + 1e-9 or ask0 > .99:
        return None, "SIZE_DEPTH_OR_NOTIONAL_CAP"
    origin_ms = int(row["decision_ns"]) / 1_000_000.0
    arrival_ms, exit_target_ms = origin_ms + latency_ms, origin_ms + exit_ms
    watermark = session.get("watermark_ms", session.get("last_wall_ms") or 0)
    if watermark < exit_target_ms:
        return None, "SESSION_WATERMARK_BEFORE_EXIT"
    arrival_pair = pair_asof_session(session, row, arrival_ms)
    exit_pair = pair_asof_session(session, row, exit_target_ms)
    if arrival_pair is None:
        return None, "ARRIVAL_ASOF_UNAVAILABLE"
    if exit_pair is None:
        return None, "EXIT_ASOF_UNAVAILABLE"
    arrival, exit_state = side_state(arrival_pair, side), side_state(exit_pair, side)
    if arrival is None:
        return None, "ARRIVAL_L1_DEPTH_UNAVAILABLE"
    if exit_state is None:
        return None, "EXIT_L1_DEPTH_UNAVAILABLE"
    if row.get("epoch") and int(arrival_pair["connection_epoch"]) != int(row["epoch"]):
        return None, "ENTRY_EPOCH_MISMATCH"
    if int(exit_pair["connection_epoch"]) != int(arrival_pair["connection_epoch"]):
        return None, "EXIT_EPOCH_MISMATCH"
    common = {
        "side": str(side),
        "arrival_state_available_ms": int(arrival_pair["state_available_wall_ms"]),
        "exit_state_available_ms": int(exit_pair["state_available_wall_ms"]),
    }
    if arrival["ask"] > ask0 + 1e-12:
        return {**common, "cash_pnl": 0.0, "filled": 0.0, "exit_filled": 0.0,
                "entry_price": None, "exit_bid": None}, "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"
    fill = min(SIZE, arrival["ask_depth"])
    if fill <= 0:
        return {**common, "cash_pnl": 0.0, "filled": 0.0, "exit_filled": 0.0,
                "entry_price": None, "exit_bid": None}, "OBSERVED_NO_FILL_ZERO_DEPTH"
    exit_fill = min(fill, exit_state["bid_depth"])
    entry_price, exit_bid = float(arrival["ask"]), float(exit_state["bid"])
    cash = (
        exit_fill * exit_bid - fill * entry_price
        - fill * fee_per_share(row, entry_price)
        - exit_fill * fee_per_share(row, exit_bid)
    )
    return {
        **common, "cash_pnl": float(cash), "filled": float(fill),
        "exit_filled": float(exit_fill), "residual_inventory": float(fill - exit_fill),
        "entry_price": entry_price, "exit_bid": exit_bid,
        "arrival_asof_gap_ms": float(arrival_ms - arrival_pair["state_available_wall_ms"]),
        "exit_asof_gap_ms": float(exit_target_ms - exit_pair["state_available_wall_ms"]),
    }, "OBSERVED_FULL_FILL" if fill + 1e-12 >= SIZE else "OBSERVED_PARTIAL_FILL"

def candidate_windows(rows):
    times = sorted(int(r["decision_ns"]) for r in rows)
    if not times or times[-1] - times[0] < WINDOW_NS:
        return []
    first = (times[0] // STEP_NS) * STEP_NS
    output = []
    for start in range(first, times[-1] - WINDOW_NS + 1, STEP_NS):
        end = start + WINDOW_NS
        subset = [r for r in rows if start <= int(r["decision_ns"]) < end]
        if subset:
            output.append({
                "start_ns": start, "end_ns": end, "rows": subset,
                "assets": len({str(r.get("asset") or "UNKNOWN") for r in subset}),
                "contracts": len({str(r.get("horizon") or "UNKNOWN") for r in subset}),
                "markets": len({str(r["market_id"]) for r in subset}),
                "decisions": len(subset),
            })
    return output

def select_window(root, rows):
    candidates = candidate_windows(rows)
    if not candidates:
        raise ValueError("NO_CONTIGUOUS_TWO_HOUR_EVIDENCE")
    top = sorted(
        candidates,
        key=lambda c: (c["assets"], c["contracts"], c["markets"], c["decisions"], -c["start_ns"]),
        reverse=True,
    )[:8]
    union = {r["decision_id"]: r for c in top for r in c["rows"]}
    probe_rows = list(union.values())
    sessions, diag = stream_sessions(Path(root).resolve().parent, probe_rows)
    if not sessions:
        sessions, fallback = jsonl_sessions(Path(root).resolve(), probe_rows)
        diag = {**diag, **fallback, "fallback": "JSONL_BOOK_OBSERVATIONS"}
    if not sessions:
        sessions, fallback = stream_raw_sessions(Path(root), probe_rows)
        diag = {**diag, **fallback, "fallback": "RAW_CAUSAL_BOOK_JSONL"}
    if not sessions:
        raise ValueError("NO_CAUSAL_PM_SESSION")
    cache = {r["decision_id"]: session_for_row(sessions, r) for r in probe_rows}
    for c in top:
        matched, reasons = 0, Counter()
        for row in c["rows"]:
            session, reason = cache[row["decision_id"]]
            if session is not None:
                matched += 1
            else:
                reasons[str(reason)] += 1
        c["pm_session_rows"] = matched
        c["pm_session_coverage"] = matched / len(c["rows"])
        c["session_exclusions"] = dict(reasons)
    winner = max(
        top,
        key=lambda c: (
            c["assets"], c["contracts"], c["pm_session_coverage"],
            c["markets"], c["decisions"], -c["start_ns"],
        ),
    )
    rows_out = winner.pop("rows")
    return winner, rows_out, sessions, diag

def chronological_split(rows):
    ordered = sorted(rows, key=lambda r: (int(r["decision_ns"]), str(r["decision_id"])))
    n = len(ordered)
    a, b = max(1, int(n * .60)), max(2, int(n * .80))
    b = min(max(a + 1, b), n)
    return {"TRAIN": ordered[:a], "VALIDATION": ordered[a:b], "LOCAL_TEST": ordered[b:]}

def equity_stats(events):
    equity = peak = max_dd = 0.0
    positive = negative = zero = 0
    path = []
    for event in sorted(events, key=lambda e: (e["decision_ns"], e["decision_id"])):
        pnl = float(event["cash_pnl"])
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        positive += pnl > 1e-15
        negative += pnl < -1e-15
        zero += abs(pnl) <= 1e-15
        path.append({"decision_ns": event["decision_ns"], "equity": equity})
    fills = len(events)
    return {
        "total_pnl": equity, "fills": fills,
        "pnl_per_fill": equity / fills if fills else None,
        "hit_rate": positive / (positive + negative) if positive + negative else None,
        "max_drawdown": max_dd, "positive": positive, "negative": negative,
        "zero": zero, "equity": path,
    }

def exact_baseline_cube(rows, session_cache):
    metrics, events = {}, defaultdict(list)
    censored = defaultdict(Counter)
    for latency in LATENCIES:
        for horizon in EXITS:
            key = f"{latency}::{horizon}"
            observed = fills = no_fills = 0
            for row in rows:
                session, reason = session_cache[row["decision_id"]]
                if session is None:
                    censored[key][str(reason)] += 1
                    continue
                economics, state = execute_side_cell(
                    row, session, latency, horizon, selected_action_side(row))
                if economics is None:
                    censored[key][state] += 1
                    continue
                observed += 1
                if float(economics.get("filled") or 0) > 0:
                    fills += 1
                    events[key].append({
                        "decision_ns": int(row["decision_ns"]),
                        "decision_id": str(row["decision_id"]),
                        "market_id": str(row["market_id"]),
                        "asset": str(row.get("asset") or "UNKNOWN"),
                        "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
                        "cash_pnl": float(economics["cash_pnl"]),
                        "execution_state": state, **economics,
                    })
                else:
                    no_fills += 1
            stats = equity_stats(events[key])
            metrics[key] = {
                "opportunities": len(rows), "observed_actions": observed,
                "fills": fills, "no_fills": no_fills, "censored": len(rows) - observed,
                **{k: v for k, v in stats.items() if k != "equity"},
            }
    return {
        "metrics": metrics,
        "equity_events": {k: sorted(v, key=lambda e:(e["decision_ns"],e["decision_id"])) for k,v in events.items()},
        "equity_paths": {k: equity_stats(v)["equity"] for k,v in events.items()},
        "censored": {k: dict(v) for k,v in censored.items()},
    }

def design_record(row, side, latency, horizon, info_keys, tape_index=None, delay_ms=0):
    state = decision_side_state(row, side)
    if state is None:
        return None
    source = row_features(row, tape_index, delay_ms)
    out = {
        "action.side_sign": 1.0 if side == "YES" else -1.0,
        "system.latency_ms": float(latency), "system.log_latency": math.log1p(float(latency)),
        "action.exit_ms": float(horizon), "action.log_exit": math.log1p(float(horizon)),
        "action.ask": float(state["ask"]), "action.bid": float(state["bid"]),
        "action.spread": float(state["ask"]) - float(state["bid"]),
        "action.depth": float(state["ask_quantity"]),
    }
    for key in info_keys:
        if key in source:
            out["x." + key] = source[key]
    out["asset::" + str(row.get("asset") or "UNKNOWN")] = 1.0
    out["contract::" + str(row.get("horizon") or "UNKNOWN")] = 1.0
    return out

class Ridge:
    def __init__(self, alpha=8.0):
        self.alpha = float(alpha)

    def fit(self, records, targets):
        self.names = sorted({k for row in records for k in row})
        if not self.names or not records:
            raise ValueError("EMPTY_RIDGE_TRAINING_SET")
        x = np.full((len(records), len(self.names)), np.nan)
        pos = {name:j for j,name in enumerate(self.names)}
        for i,row in enumerate(records):
            for name,value in row.items():
                x[i,pos[name]] = float(value)
        missing = ~np.isfinite(x)
        count = np.sum(~missing, axis=0)
        self.center = np.divide(np.nansum(x,axis=0),count,out=np.zeros(len(self.names)),where=count>0)
        filled = np.where(missing,self.center[None,:],x)
        self.scale = np.std(filled,axis=0)
        self.scale = np.where(self.scale>1e-9,self.scale,1.0)
        z = (filled-self.center[None,:])/self.scale[None,:]
        z = np.hstack([np.ones((len(records),1)),z,missing.astype(float)])
        y = np.asarray(targets,dtype=float)
        penalty = np.eye(z.shape[1])*self.alpha
        penalty[0,0] = 0.0
        self.beta = np.linalg.solve(z.T@z+penalty,z.T@y)
        self.training_rows, self.dimension = len(records), z.shape[1]
        return self

    def predict(self, records):
        if not records:
            return np.asarray([])
        x = np.full((len(records),len(self.names)),np.nan)
        pos={name:j for j,name in enumerate(self.names)}
        for i,row in enumerate(records):
            for name,value in row.items():
                j=pos.get(name)
                if j is not None and finite(value):
                    x[i,j]=float(value)
        missing=~np.isfinite(x)
        filled=np.where(missing,self.center[None,:],x)
        z=(filled-self.center[None,:])/self.scale[None,:]
        z=np.hstack([np.ones((len(records),1)),z,missing.astype(float)])
        return z@self.beta

def training_examples(rows, session_cache, info_keys, tape_index):
    records, targets, states = [], [], Counter()
    for row in rows:
        session, reason = session_cache[row["decision_id"]]
        if session is None:
            states[str(reason)] += 1
            continue
        for latency in LATENCIES:
            for horizon in EXITS:
                for side in decision_action_sides(row):
                    design=design_record(row,side,latency,horizon,info_keys,tape_index,0)
                    if design is None:
                        continue
                    economics,state=execute_side_cell(row,session,latency,horizon,side)
                    states[state]+=1
                    if economics is not None:
                        records.append(design)
                        targets.append(float(economics["cash_pnl"]))
    return records,targets,dict(states)

def evaluate_model(model, rows, session_cache, info_keys, tape_index, delay_ms=0):
    metrics,events = {},defaultdict(list)
    censored,decisions = defaultdict(Counter),defaultdict(Counter)
    for latency in LATENCIES:
        for horizon in EXITS:
            key=f"{latency}::{horizon}"
            for row in rows:
                session,reason=session_cache[row["decision_id"]]
                if session is None:
                    censored[key][str(reason)]+=1
                    continue
                candidates,sides=[],[]
                for side in decision_action_sides(row):
                    rec=design_record(row,side,latency,horizon,info_keys,tape_index,delay_ms)
                    if rec is not None:
                        candidates.append(rec);sides.append(side)
                if not candidates:
                    censored[key]["NO_SIDE_FEATURE_STATE"]+=1
                    continue
                pred=model.predict(candidates)
                best=int(np.argmax(pred))
                if not finite(float(pred[best])) or float(pred[best])<=0:
                    decisions[key]["NO_TRADE"]+=1
                    continue
                side=sides[best]
                economics,state=execute_side_cell(row,session,latency,horizon,side)
                if economics is None:
                    censored[key][state]+=1
                    continue
                decisions[key][side]+=1
                if float(economics.get("filled") or 0)>0:
                    events[key].append({
                        "decision_ns":int(row["decision_ns"]),"decision_id":str(row["decision_id"]),
                        "market_id":str(row["market_id"]),"asset":str(row.get("asset") or "UNKNOWN"),
                        "contract_horizon":str(row.get("horizon") or "UNKNOWN"),
                        "cash_pnl":float(economics["cash_pnl"]),"prediction":float(pred[best]),
                        "execution_state":state,**economics,
                    })
            stats=equity_stats(events[key])
            trades=decisions[key]["YES"]+decisions[key]["NO"]
            metrics[key]={
                "opportunities":len(rows),"trade_count":int(trades),
                "no_trade_count":int(decisions[key]["NO_TRADE"]),"fills":stats["fills"],
                "total_pnl":stats["total_pnl"],
                "pnl_per_trade":stats["total_pnl"]/trades if trades else None,
                "pnl_per_fill":stats["pnl_per_fill"],
                "pnl_per_opportunity":stats["total_pnl"]/len(rows) if rows else None,
                "hit_rate":stats["hit_rate"],"max_drawdown":stats["max_drawdown"],
                "yes_trades":int(decisions[key]["YES"]),"no_trades":int(decisions[key]["NO"]),
                "censored":int(sum(censored[key].values())),
            }
    return {
        "metrics":metrics,"events":{k:sorted(v,key=lambda e:(e["decision_ns"],e["decision_id"])) for k,v in events.items()},
        "censored":{k:dict(v) for k,v in censored.items()},
        "decisions":{k:dict(v) for k,v in decisions.items()},
    }

def delta_cube(model_result, baseline):
    return {
        key: float(value["total_pnl"]) - float(baseline["metrics"][key]["total_pnl"])
        for key,value in model_result["metrics"].items()
        if key in baseline["metrics"]
    }

def best_cell(metrics):
    valid=[(k,v) for k,v in metrics.items() if finite(v.get("total_pnl"))]
    return max(valid,key=lambda kv:float(kv[1]["total_pnl"])) if valid else (None,None)

def robust_score(delta):
    vals=[float(v) for v in delta.values() if finite(v)]
    return {
        "cells":len(vals),"positive_cells":sum(v>0 for v in vals),
        "positive_fraction":sum(v>0 for v in vals)/len(vals) if vals else None,
        "median_delta_pnl":statistics.median(vals) if vals else None,
        "mean_delta_pnl":statistics.fmean(vals) if vals else None,
        "max_delta_pnl":max(vals) if vals else None,
    }

def benchmark_model(model,sample):
    if not sample:
        return {"state":"INSUFFICIENT_DATA"}
    timings=[]
    for _ in range(200):
        start=time.perf_counter_ns();model.predict(sample);timings.append(time.perf_counter_ns()-start)
    timings.sort()
    q=lambda p:timings[min(len(timings)-1,int((len(timings)-1)*p))]
    return {
        "state":"READY","batch_size":len(sample),"p50_ns":q(.5),"p90_ns":q(.9),
        "p99_ns":q(.99),"p99_9_ns":q(.999),"per_action_p50_ns":q(.5)/len(sample),
        "model_dimension":int(model.dimension),"training_rows":int(model.training_rows),
    }

def classify(features_added,score,curve):
    if not features_added or not score.get("cells"):
        return "INSUFFICIENT_DATA"
    if (score.get("median_delta_pnl") or 0)<=0 or (score.get("positive_fraction") or 0)<.55:
        return "REJECT_2H_SCREEN"
    zero,slow=curve.get("0"),curve.get("100")
    if finite(zero) and finite(slow) and abs(float(zero))>1e-12 and float(slow)<.5*float(zero):
        return "PROMISING_2H_FAST"
    return "PROMISING_2H_ASYNC"

def write_json(path,value):
    atomic_json(path,value)

def run(root,output_dir,minimum_wall_ns,baseline_code_sha,source_sha):
    output_dir.mkdir(parents=True,exist_ok=True)
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":
        raise ValueError("BASE_DATA_NOT_READY:"+str(data.get("input_state")))
    rows=[r for r in data["decisions"] if _valid_state(r)]
    if not rows: raise ValueError("NO_VALID_CAUSAL_DECISIONS")
    window,window_rows,sessions,tape_diag=select_window(root,rows)
    start_ns,end_ns=int(window["start_ns"]),int(window["end_ns"])
    if end_ns-start_ns!=WINDOW_NS: raise AssertionError("window not exactly two hours")
    session_cache={r["decision_id"]:session_for_row(sessions,r) for r in window_rows}
    splits=chronological_split(window_rows)
    tape_index,feature_diag=load_feature_tape(root,start_ns,end_ns)
    nested,available=nested_family_keys(window_rows,tape_index)
    baseline_all=exact_baseline_cube(window_rows,session_cache)
    baseline_test=exact_baseline_cube(splits["LOCAL_TEST"],session_cache)

    write_json(output_dir/"01_2h_manifest.json",{
        "schema":SCHEMA+"_manifest_v1",**SAFETY,"automatic_promotion":False,
        "window_start_ns":start_ns,"window_end_ns":end_ns,"window_duration_seconds":7200,
        "selection_policy":"DATA_QUALITY_ONLY_NO_PNL","selection_receipt":window,
        "source_sha":source_sha,"baseline_code_sha":baseline_code_sha,
        "data_sha256":data.get("data_sha256"),"source_files":data.get("sources"),
        "latencies_ms":list(LATENCIES),"exit_horizons_ms":list(EXITS),"target_size_shares":SIZE,
    })
    write_json(output_dir/"02_data_coverage.json",{
        "schema":SCHEMA+"_coverage_v1",**SAFETY,
        "assets":sorted({str(r.get("asset") or "UNKNOWN") for r in window_rows}),
        "contract_horizons":sorted({str(r.get("horizon") or "UNKNOWN") for r in window_rows}),
        "opportunities":len(window_rows),"markets":len({str(r["market_id"]) for r in window_rows}),
        "split_rows":{k:len(v) for k,v in splits.items()},
        "pm_tape_diagnostics":tape_diag,"feature_tape_diagnostics":feature_diag,
        "available_feature_keys":list(available),
    })
    write_json(output_dir/"03_baseline_manifest.json",{
        "schema":SCHEMA+"_baseline_manifest_v1",**SAFETY,
        "baseline_code_sha":baseline_code_sha,
        "baseline_strategy_definition":"CURRENT_ALL_CRYPTO_COMPACT_TIMING_EQUITY_SELECTED_SIDE",
        "execution_semantics":"EXECUTABLE_ASK_ENTRY_EXECUTABLE_BID_EXIT_L1_DEPTH_FEES_ZERO_CHASE",
        "missing_semantics":"CENSORED_NEVER_ZERO","latencies_ms":list(LATENCIES),
        "exit_horizons_ms":list(EXITS),"size_shares":SIZE,"data_sha256":data.get("data_sha256"),
        "window_start_ns":start_ns,"window_end_ns":end_ns,
    })
    write_json(output_dir/"04_baseline_2h.json",{"schema":SCHEMA+"_baseline_2h_v1",**SAFETY,"full_2h":baseline_all,"local_test_comparator":baseline_test})
    write_json(output_dir/"05_external_backfill.json",{
        "schema":SCHEMA+"_external_inventory_v1",**SAFETY,
        "captured_causal_feature_tape":feature_diag,
        "historical_backfill_policy":"PUBLIC_HISTORY_WHEN_RECEIVE_TIME_NOT_REQUIRED",
        "arrival_timing_policy":"RECORDED_LONDON_RECEIVE_OR_FEATURE_READY_TIME_FOR_LOW_LATENCY_CAUSALITY",
        "note":"First pass uses already-recorded causal external features; absent families are INSUFFICIENT_DATA, never fabricated.",
    })
    write_json(output_dir/"06_causal_join.json",{
        "schema":SCHEMA+"_causal_join_v1",**SAFETY,"join":"BACKWARD_ASOF_ONLY",
        "feature_ready_ns":"max(available_at_ns,decision_wall_ns,recorded_wall_ns)",
        "delay_grid_ms":list(DELAYS_MS),"future_nearest_allowed":False,"feature_markets":len(tape_index),
    })

    models,results,scorecard={}, {}, []
    previous=set()
    for family,_ in FAMILIES:
        keys=nested[family];added=sorted(set(keys)-previous);previous=set(keys)
        if family not in ("F0_PM_ONLY","F1_BASELINE") and not added:
            scorecard.append({"family":family,"features_added":[],"status":"INSUFFICIENT_DATA","median_delta_pnl":None,"positive_fraction":None})
            continue
        try:
            records,targets,states=training_examples(splits["TRAIN"],session_cache,keys,tape_index)
            model=Ridge(8.0).fit(records,targets)
        except (ValueError,np.linalg.LinAlgError):
            scorecard.append({"family":family,"features_added":added,"status":"INSUFFICIENT_DATA","median_delta_pnl":None,"positive_fraction":None})
            continue
        models[family]=model
        validation=evaluate_model(model,splits["VALIDATION"],session_cache,keys,tape_index,0)
        test=evaluate_model(model,splits["LOCAL_TEST"],session_cache,keys,tape_index,0)
        delta=delta_cube(test,baseline_test);score=robust_score(delta)
        sample=[]
        for row in splits["LOCAL_TEST"][:16]:
            rec=design_record(row,"YES",50,1000,keys,tape_index,0)
            if rec: sample.append(rec)
        results[family]={
            "features":list(keys),"features_added":added,"training_examples":len(records),
            "training_state_counts":states,"validation":validation,"local_test":test,
            "delta_vs_exact_baseline_local_test":delta,"robustness":score,
            "inference_benchmark":benchmark_model(model,sample),
        }
        scorecard.append({"family":family,"features_added":added,"status":"PENDING_DELAY_CLASSIFICATION","median_delta_pnl":score.get("median_delta_pnl"),"positive_fraction":score.get("positive_fraction")})

    write_json(output_dir/"07_univariate_alpha.json",{"schema":SCHEMA+"_univariate_v1",**SAFETY,"results":{k:{"features_added":v["features_added"],"robustness":v["robustness"],"best_cell":best_cell(v["local_test"]["metrics"])[0]} for k,v in results.items()}})
    write_json(output_dir/"08_nested_models.json",{"schema":SCHEMA+"_nested_models_v1",**SAFETY,"models":results,"internal_test_label":"2H_INTERNAL_RESEARCH_TEST"})

    info_latency={}
    for family,model in models.items():
        zero=results[family]["local_test"];cell,_=best_cell(zero["metrics"]);curve={}
        if cell:
            for delay in DELAYS_MS:
                if delay and not tape_index:
                    curve[str(delay)]=None
                else:
                    delayed=evaluate_model(model,splits["LOCAL_TEST"],session_cache,nested[family],tape_index,delay)
                    curve[str(delay)]=delayed["metrics"][cell]["total_pnl"]
        info_latency[family]=curve
    by_family={r["family"]:r for r in scorecard}
    for family,row in by_family.items():
        row["status"]=classify(row.get("features_added") or [],results.get(family,{}).get("robustness",{}),info_latency.get(family,{}))

    write_json(output_dir/"09_information_latency.json",{
        "schema":SCHEMA+"_information_latency_v1",**SAFETY,"curves":info_latency,
        "IG_definition":"PnL(F,0)-PnL(EXACT_BASELINE,0)",
        "LC_definition":"PnL(F,0)-PnL(F,d)",
        "NIV_definition":"PnL(F,d)-PnL(F1,d); exact BASELINE_V0 is separately reported",
    })
    write_json(output_dir/"10_entry_exit_information_cube.json",{
        "schema":SCHEMA+"_cube_v1",**SAFETY,"exact_baseline_local_test":baseline_test["metrics"],
        "models":{k:{"metrics":v["local_test"]["metrics"],"delta_vs_exact_baseline":v["delta_vs_exact_baseline_local_test"]} for k,v in results.items()},
    })
    write_json(output_dir/"11_signal_decay.json",{
        "schema":SCHEMA+"_signal_decay_v1",**SAFETY,
        "curves":{family:{str(h):statistics.fmean(v["local_test"]["metrics"][f"{l}::{h}"]["total_pnl"] for l in LATENCIES) for h in EXITS} for family,v in results.items()},
    })
    write_json(output_dir/"12_momentum_reversal.json",{
        "schema":SCHEMA+"_momentum_reversal_v1",**SAFETY,
        "actions":{family:{
            "YES":sum(m["yes_trades"] for m in v["local_test"]["metrics"].values()),
            "NO":sum(m["no_trades"] for m in v["local_test"]["metrics"].values()),
            "NO_TRADE":sum(m["no_trade_count"] for m in v["local_test"]["metrics"].values()),
        } for family,v in results.items()},
    })

    dynamic={"schema":SCHEMA+"_dynamic_exit_v1",**SAFETY,"state":"INSUFFICIENT_DATA"}
    try:
        dm=DynamicExitValueModel().fit(splits["TRAIN"])
        dynamic={"schema":SCHEMA+"_dynamic_exit_v1",**SAFETY,"state":"READY","training_receipt":dm.training_receipt,"diagnostic":summarize_dynamic_exit(dm,splits["LOCAL_TEST"],position_size=SIZE),"evidence_note":"Native causal repricing labels; reported separately from compact-book baseline economics."}
    except (ValueError,RuntimeError):
        pass
    write_json(output_dir/"13_dynamic_exit.json",dynamic)
    write_json(output_dir/"14_feature_costs.json",{
        "schema":SCHEMA+"_feature_costs_v1",**SAFETY,
        "models":{k:v["inference_benchmark"] for k,v in results.items()},
        "critical_path_contract":"NO_HTTP_NO_REST_NO_FILESYSTEM_NO_DB_NO_PYTHON_IPC_NO_SYNCHRONOUS_WAIT",
        "note":"Offline Python inference is a screening proxy; production hot path remains C++.",
    })

    ranked=[(k,v["robustness"].get("median_delta_pnl")) for k,v in results.items() if finite(v["robustness"].get("median_delta_pnl"))]
    ranked.sort(key=lambda kv:float(kv[1]),reverse=True)
    best_family=ranked[0][0] if ranked else None
    best_result=results.get(best_family,{}).get("local_test") if best_family else None
    write_json(output_dir/"15_equities.json",{"schema":SCHEMA+"_equities_v1",**SAFETY,"baseline":baseline_all["equity_paths"],"best_family":best_family,"best_enriched":(best_result or {}).get("events",{})})
    write_json(output_dir/"16_feature_scorecard.json",{"schema":SCHEMA+"_scorecard_v1",**SAFETY,"rows":scorecard})
    promising=[r["family"] for r in scorecard if str(r.get("status","")).startswith("PROMISING_2H")]
    shortlist=["BASELINE_V0"]+promising[:3]
    write_json(output_dir/"17_shortlist.json",{"schema":SCHEMA+"_shortlist_v1",**SAFETY,"candidates":shortlist,"maximum_nonbaseline_candidates":3,"automatic_promotion":False,"next_stage":"LONGER_HISTORICAL_ROBUSTNESS_THEN_FROZEN_FUTURE_PAPER_OOS"})
    write_json(output_dir/"18_rejected_features.json",{"schema":SCHEMA+"_rejections_v1",**SAFETY,"rejected":[r for r in scorecard if r["status"]=="REJECT_2H_SCREEN"],"insufficient":[r for r in scorecard if r["status"]=="INSUFFICIENT_DATA"]})

    bkey,bval=best_cell(baseline_all["metrics"]);rkey,rval=best_cell((best_result or {}).get("metrics",{}))
    (output_dir/"00_executive_summary.md").write_text(
        "# Multi-alpha 2H research\n\n"
        "**Status:** 2H INTERNAL RESEARCH TEST. Not final OOS evidence.\n\n"
        f"- Window: {start_ns} to {end_ns} (exactly 2 hours).\n"
        f"- Opportunities: {len(window_rows)}.\n"
        f"- Exact baseline best cell: {bkey}; PnL {None if bval is None else bval.get('total_pnl')}.\n"
        f"- Best enriched family on local test: {best_family}; cell {rkey}; PnL {None if rval is None else rval.get('total_pnl')}.\n"
        f"- Shortlist: {', '.join(shortlist)}.\n\n"
        "Window selection used data quality and causal coverage only. PnL was not used.\n"
        "Missing/censored evidence remains missing. No automatic promotion or real execution is possible.\n",
        encoding="utf-8",
    )
    (output_dir/"19_next_stage_plan.md").write_text(
        "# Next stage\n\n"
        "1. Freeze shortlisted feature sets, model form, size, latency and exit semantics.\n"
        "2. Run only those candidates on a substantially longer historical window.\n"
        "3. Reject isolated-cell or unstable effects.\n"
        "4. Freeze the final candidate and collect untouched future PAPER OOS with no tuning.\n"
        "5. Keep pure arbitrage isolated from every statistical feature/model path.\n",
        encoding="utf-8",
    )
    return {"schema":SCHEMA,**SAFETY,"state":"READY","window_start_ns":start_ns,"window_end_ns":end_ns,"baseline_best_cell":bkey,"best_enriched_family":best_family,"best_enriched_cell":rkey,"shortlist":shortlist,"output_directory":str(output_dir)}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--minimum-wall-ns",type=int,required=True)
    parser.add_argument("--baseline-code-sha",required=True)
    parser.add_argument("--source-sha",required=True)
    args=parser.parse_args(argv)
    for name in ("baseline_code_sha","source_sha"):
        value=getattr(args,name)
        if len(value)!=40 or any(ch not in "0123456789abcdef" for ch in value):
            parser.error(name+" must be exact 40-hex SHA")
    result=run(args.root,args.output_dir,args.minimum_wall_ns,args.baseline_code_sha,args.source_sha)
    print("MULTI_ALPHA_2H_READY="+json.dumps(result,sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
