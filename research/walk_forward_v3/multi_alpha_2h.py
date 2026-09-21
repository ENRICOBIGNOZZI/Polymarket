"""Fast 2H multi-alpha discovery for the all-crypto Polymarket PAPER lane.

The program is intentionally research-only. It freezes a contiguous two-hour
window using data-quality criteria only, reproduces the current all-crypto
continuous-L1 timing baseline, then screens nested information sets with the
existing direct executable-cash-PnL learner. All rich-state joins are backward
as-of on feature availability time. Missing execution evidence is censored,
never imputed as zero.

No production configuration, execution authority, or pure-arbitrage path is
imported or modified by this module.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Iterable

from research.walk_forward_v2.core import (
    SAFETY,
    atomic_json,
    build_dataset,
    fee_per_share,
    feature_names,
    finite,
)
from research.walk_forward_v3 import direct_action as da
from research.walk_forward_v3.btc_compact_equity import (
    EXITS,
    LATENCIES,
    SIZE,
    execute_cell,
    jsonl_sessions,
    pair_asof_session,
    side_state,
    stream_sessions,
)
from research.walk_forward_v3.dynamic_exit import (
    DynamicExitValueModel,
    summarize_dynamic_exit,
)

SCHEMA = "polymarket_v7_multi_alpha_2h_v1"
WINDOW_NS = 2 * 60 * 60 * 1_000_000_000
BIN_NS = 5 * 60 * 1_000_000_000
DELAY_GRID_MS = (0, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000)
BASELINE_ORIGIN_COMMIT = "7411021bf2ae7bf052fc4f0f540433f9d90ff5e5"
BASELINE_MODULE = "research/walk_forward_v3/all_crypto_compact_equity.py"
BASELINE_KERNEL_MODULE = "research/walk_forward_v3/btc_compact_equity.py"

FAMILY_ORDER = (
    "baseline",
    "cross_venue",
    "flow",
    "ofi",
    "perp",
    "oi_funding",
    "liquidations",
    "volatility",
    "cross_asset",
    "options",
    "settlement",
)
FAMILY_LABEL = {
    "baseline": "Current lead-lag",
    "cross_venue": "Cross venue",
    "flow": "Trade flow",
    "ofi": "OFI",
    "perp": "Perp",
    "oi_funding": "OI/Funding",
    "liquidations": "Liquidations",
    "volatility": "Volatility",
    "cross_asset": "Cross asset",
    "options": "Deribit/options",
    "settlement": "Settlement",
    "pm_response": "PM response",
}
NESTED = {
    "F0": frozenset(),
    "F1": frozenset(("baseline",)),
    "F2": frozenset(("baseline", "cross_venue")),
    "F3": frozenset(("baseline", "cross_venue", "flow")),
    "F4": frozenset(("baseline", "cross_venue", "flow", "ofi")),
    "F5": frozenset(("baseline", "cross_venue", "flow", "ofi", "perp")),
    "F6": frozenset(("baseline", "cross_venue", "flow", "ofi", "perp", "oi_funding")),
    "F7": frozenset(("baseline", "cross_venue", "flow", "ofi", "perp", "oi_funding", "liquidations")),
    "F8": frozenset(("baseline", "cross_venue", "flow", "ofi", "perp", "oi_funding", "liquidations", "volatility")),
    "F9": frozenset(("baseline", "cross_venue", "flow", "ofi", "perp", "oi_funding", "liquidations", "volatility", "cross_asset")),
    "F10": frozenset(("baseline", "cross_venue", "flow", "ofi", "perp", "oi_funding", "liquidations", "volatility", "cross_asset", "options")),
    "F11": frozenset(("baseline", "cross_venue", "flow", "ofi", "perp", "oi_funding", "liquidations", "volatility", "cross_asset", "options", "settlement")),
}
TIER1 = ("cross_venue", "flow", "pm_response", "volatility", "cross_asset", "perp")

SAFETY_PLUS = {
    **SAFETY,
    "automatic_promotion": False,
    "execution_authority": False,
    "research_only": True,
}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def robust_json_lines(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            magic = handle.read(2)
    except OSError:
        return
    opener = gzip.open if magic == b"\x1f\x8b" else open
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(row, dict):
                    yield row
    except (OSError, UnicodeDecodeError):
        return


def _flatten(prefix: str, value: Any, out: dict[str, float]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(prefix + "." + str(key) if prefix else str(key), child, out)
    elif finite(value):
        out[prefix] = float(value)


def discover_feature_tapes(root: Path) -> list[Path]:
    """Bounded filename-based discovery; contents are schema-validated later."""
    root = root.resolve()
    bases = [root]
    if root.name == "hft_permanent":
        bases.append(root.parent)
    elif (root / "research" / "hft_permanent").is_dir():
        bases.append(root / "research")
    patterns = (
        "**/*feature*tape*.jsonl",
        "**/*feature*tape*.jsonl.gz",
        "**/*multi_crypto_feature*.jsonl",
        "**/*multi_crypto_feature*.jsonl.gz",
    )
    found: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for pattern in patterns:
            for path in base.glob(pattern):
                if path.is_file() and not path.is_symlink():
                    found.add(path.resolve())
    return sorted(found)


def load_feature_tape(
    paths: Iterable[Path],
    *,
    start_ns: int,
    end_ns: int,
    lookback_ns: int = 2_000_000_000,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Index causal rich-state snapshots by market and feature-ready time."""
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    counts = Counter()
    files = []
    lower = int(start_ns) - int(lookback_ns)
    upper = int(end_ns)
    for path in sorted({Path(p).resolve() for p in paths}):
        files.append(str(path))
        for row in robust_json_lines(path):
            if row.get("schema") != "polymarket_v7_multi_crypto_feature_tape_v1":
                continue
            counts["schema_rows"] += 1
            if not (
                row.get("paper_only") is True
                and row.get("authenticated_execution") is False
                and row.get("real_order_submission") is False
                and row.get("execution_authority") is False
            ):
                counts["authority_rejected"] += 1
                continue
            available = row.get("available_at_ns")
            decision = row.get("decision_wall_ns")
            market = str(row.get("market_id") or "")
            if not isinstance(available, int) or not isinstance(decision, int) or not market:
                counts["clock_or_identity_rejected"] += 1
                continue
            if available <= 0 or available > decision:
                counts["future_availability_rejected"] += 1
                continue
            if available < lower or available > upper:
                counts["outside_window"] += 1
                continue
            expected = str(row.get("record_hash") or "")
            if expected:
                unhashed = dict(row)
                unhashed.pop("record_hash", None)
                if canonical_hash(unhashed) != expected:
                    counts["hash_rejected"] += 1
                    continue
            features: dict[str, float] = {}
            _flatten("tape", row.get("features") or {}, features)
            features["tape.feature_age_ms"] = 0.0
            by_market[market].append({
                "available_at_ns": int(available),
                "features": features,
                "source_identity_hash": row.get("source_identity_hash"),
                "feature_schema_hash": row.get("feature_schema_hash"),
                "model_sha": row.get("model_sha"),
            })
            counts["accepted"] += 1
    indexed: dict[str, dict[str, Any]] = {}
    for market, seq in by_market.items():
        seq.sort(key=lambda item: item["available_at_ns"])
        indexed[market] = {
            "rows": seq,
            "stamps": [item["available_at_ns"] for item in seq],
        }
    return indexed, {
        "files": files,
        "file_count": len(files),
        "markets": len(indexed),
        "counts": dict(sorted(counts.items())),
    }


def attach_rich_state(
    rows: Iterable[dict[str, Any]],
    feature_index: dict[str, dict[str, Any]],
    *,
    delay_ms: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Backward as-of only: feature ready time must be <= decision-delay."""
    delay_ns = int(delay_ms) * 1_000_000
    out = []
    joined = 0
    ages = []
    start = time.perf_counter_ns()
    for original in rows:
        row = dict(original)
        row["features"] = dict(original.get("features") or {})
        market = str(row.get("market_id") or "")
        cutoff = int(row["decision_ns"]) - delay_ns
        index = feature_index.get(market)
        selected = None
        if index is not None:
            pos = bisect_right(index["stamps"], cutoff) - 1
            if pos >= 0:
                selected = index["rows"][pos]
        if selected is not None:
            age_ms = (int(row["decision_ns"]) - int(selected["available_at_ns"])) / 1e6
            if age_ms >= float(delay_ms) - 1e-9:
                row["features"].update(selected["features"])
                row["features"]["tape.feature_age_ms"] = float(age_ms)
                row["rich_feature_available_at_ns"] = int(selected["available_at_ns"])
                joined += 1
                ages.append(age_ms)
        out.append(row)
    elapsed = time.perf_counter_ns() - start
    return out, {
        "delay_ms": int(delay_ms),
        "rows": len(out),
        "joined_rows": joined,
        "join_rate": joined / len(out) if out else None,
        "age_ms_p50": statistics.median(ages) if ages else None,
        "age_ms_p90": _quantile(ages, 0.90),
        "lookup_total_ns": int(elapsed),
        "lookup_ns_per_row": elapsed / len(out) if out else None,
        "semantics": "BACKWARD_ASOF_AVAILABLE_AT_NS_LE_DECISION_MINUS_DELAY",
    }


def _quantile(values: Iterable[float], q: float) -> float | None:
    values = sorted(float(v) for v in values if finite(v))
    if not values:
        return None
    index = int(math.ceil(float(q) * len(values))) - 1
    return values[max(0, min(len(values) - 1, index))]


def build_market_session_index(sessions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for session in sessions:
        markets = set()
        if isinstance(session.get("indexed"), dict):
            markets.update(str(key[0]) for key in session["indexed"] if isinstance(key, tuple))
        if isinstance(session.get("raw_index"), dict):
            markets.update(str(key[0]) for key in session["raw_index"] if isinstance(key, tuple))
        for market in markets:
            index[market].append(session)
    return dict(index)


def resolve_session(
    row: dict[str, Any],
    market_index: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, str | None]:
    origin_ms = int(row["decision_ns"]) / 1_000_000.0
    candidates = []
    for session in market_index.get(str(row["market_id"]), ()):
        watermark = session.get("watermark_ms", session.get("last_wall_ms") or 0)
        first = session.get("first_wall_ms")
        last = session.get("last_wall_ms")
        if first is not None and origin_ms < first:
            continue
        if last is not None and origin_ms > last:
            continue
        if watermark < origin_ms:
            continue
        if pair_asof_session(session, row, origin_ms) is not None:
            candidates.append(session)
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, "NO_CONTINUOUS_BOOK_SESSION"
    return None, "OVERLAPPING_BOOK_SESSIONS"


def _quality_external(row: dict[str, Any]) -> bool:
    features = row.get("features") or {}
    return any(
        finite(features.get(name))
        for name in (
            "external.binance_return_100ms_bp",
            "external.coinbase_return_100ms_bp",
            "external.bybit_return_100ms_bp",
            "binance_return_100ms_bp",
            "coinbase_return_100ms_bp",
            "bybit_return_100ms_bp",
            "signal_return_bp",
        )
    )


def select_two_hour_window(
    rows: list[dict[str, Any]],
    session_cache: dict[str, tuple[dict[str, Any] | None, str | None]],
) -> dict[str, Any]:
    """Choose by coverage/health only. No PnL or future-return field is read."""
    rows = sorted(rows, key=lambda row: (int(row["decision_ns"]), str(row["decision_id"])))
    if not rows:
        raise ValueError("NO_CAUSAL_DECISIONS")
    minimum = int(rows[0]["decision_ns"])
    maximum = int(rows[-1]["decision_ns"])
    if maximum - minimum < WINDOW_NS:
        raise ValueError("LESS_THAN_TWO_HOURS_OF_CAUSAL_DECISIONS")
    first = ((minimum + BIN_NS - 1) // BIN_NS) * BIN_NS
    last = maximum - WINDOW_NS
    starts = list(range(first, last + 1, BIN_NS))
    if not starts:
        starts = [minimum]
    all_assets = sorted({str(row.get("asset") or "UNKNOWN") for row in rows})
    all_horizons = sorted({str(row.get("horizon") or "UNKNOWN") for row in rows})
    best = None
    diagnostics = []
    for start in starts:
        end = start + WINDOW_NS
        selected = [row for row in rows if start <= int(row["decision_ns"]) < end]
        if not selected:
            continue
        assets = {str(row.get("asset") or "UNKNOWN") for row in selected}
        horizons = {str(row.get("horizon") or "UNKNOWN") for row in selected}
        covered = sum(
            session_cache[str(row["decision_id"])][0] is not None for row in selected
        )
        external = sum(_quality_external(row) for row in selected)
        occupied = {
            min(23, max(0, (int(row["decision_ns"]) - start) // BIN_NS))
            for row in selected
        }
        score = (
            len(assets),
            len(horizons),
            covered / len(selected),
            len(occupied),
            external / len(selected),
            len(selected),
        )
        item = {
            "start_ns": int(start),
            "end_ns": int(end),
            "rows": len(selected),
            "assets": sorted(assets),
            "contract_horizons": sorted(horizons),
            "continuous_pm_rows": int(covered),
            "continuous_pm_rate": covered / len(selected),
            "external_feature_rows": int(external),
            "external_feature_rate": external / len(selected),
            "occupied_5m_bins": len(occupied),
            "score": list(score),
        }
        diagnostics.append(item)
        if best is None or score > tuple(best["score"]):
            best = item
    if best is None:
        raise ValueError("NO_TWO_HOUR_WINDOW")
    best = dict(best)
    best["selection_rule"] = (
        "LEXICOGRAPHIC_DATA_QUALITY_ONLY:"
        "ASSET_COVERAGE,HORIZON_COVERAGE,CONTINUOUS_PM_RATE,"
        "5M_BIN_OCCUPANCY,EXTERNAL_FEATURE_RATE,ROW_COUNT"
    )
    best["profitability_used_for_selection"] = False
    best["available_assets_in_source"] = all_assets
    best["available_contract_horizons_in_source"] = all_horizons
    best["candidate_windows"] = len(diagnostics)
    best["top_quality_candidates"] = sorted(
        diagnostics, key=lambda item: tuple(item["score"]), reverse=True
    )[:10]
    return best


def split_rows(rows: list[dict[str, Any]], start_ns: int) -> dict[str, list[dict[str, Any]]]:
    train_end = int(start_ns + 0.60 * WINDOW_NS)
    validation_end = int(start_ns + 0.80 * WINDOW_NS)
    end = int(start_ns + WINDOW_NS)
    return {
        "TRAIN_60": [row for row in rows if start_ns <= int(row["decision_ns"]) < train_end],
        "VALIDATION_20": [row for row in rows if train_end <= int(row["decision_ns"]) < validation_end],
        "LOCAL_TEST_20": [row for row in rows if validation_end <= int(row["decision_ns"]) < end],
    }


def maximum_drawdown(events: list[dict[str, Any]]) -> float | None:
    if not events:
        return None
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for event in sorted(events, key=lambda item: (item["decision_ns"], item["decision_id"])):
        equity += float(event["cash_pnl"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return float(drawdown)


def finish_stats(stats: dict[str, Any]) -> dict[str, Any]:
    opportunities = int(stats.get("opportunities", 0))
    observed = int(stats.get("observed_actions", 0))
    fills = int(stats.get("fills", 0))
    trades = int(stats.get("trades", observed))
    pnl = float(stats.get("total_pnl", 0.0))
    positive = int(stats.get("positive_fills", 0))
    events = list(stats.get("events", ()))
    return {
        "opportunities": opportunities,
        "observed_actions": observed,
        "trades": trades,
        "fills": fills,
        "censored_observations": int(stats.get("censored", 0)),
        "no_fills": int(stats.get("no_fills", 0)),
        "total_pnl": pnl if observed else None,
        "pnl_per_trade": pnl / trades if trades else None,
        "pnl_per_observed_action": pnl / observed if observed else None,
        "pnl_per_fill": pnl / fills if fills else None,
        "drawdown": maximum_drawdown(events),
        "hit_rate": positive / fills if fills else None,
        "positive_fills": positive,
        "negative_fills": int(stats.get("negative_fills", 0)),
        "zero_pnl_fills": int(stats.get("zero_fills", 0)),
        "censoring_reasons": dict(sorted((stats.get("censoring_reasons") or {}).items())),
    }


def baseline_surface(
    rows: list[dict[str, Any]],
    session_cache: dict[str, tuple[dict[str, Any] | None, str | None]],
) -> dict[str, Any]:
    cells = {}
    equity = {}
    by_asset: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_horizon: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for latency in LATENCIES:
        for horizon in EXITS:
            key = f"{latency}::{horizon}"
            stats = {
                "opportunities": len(rows), "observed_actions": 0, "trades": 0,
                "fills": 0, "censored": 0, "no_fills": 0, "total_pnl": 0.0,
                "positive_fills": 0, "negative_fills": 0, "zero_fills": 0,
                "events": [], "censoring_reasons": Counter(),
            }
            for row in rows:
                session, session_reason = session_cache[str(row["decision_id"])]
                if session is None:
                    stats["censored"] += 1
                    stats["censoring_reasons"][str(session_reason)] += 1
                    continue
                economics, state = execute_cell(row, session, latency, horizon)
                if economics is None:
                    stats["censored"] += 1
                    stats["censoring_reasons"][str(state)] += 1
                    continue
                stats["observed_actions"] += 1
                stats["trades"] += 1
                pnl = float(economics["cash_pnl"])
                stats["total_pnl"] += pnl
                fill = float(economics.get("filled") or 0.0)
                if fill <= 0:
                    stats["no_fills"] += 1
                else:
                    stats["fills"] += 1
                    if pnl > 1e-15:
                        stats["positive_fills"] += 1
                    elif pnl < -1e-15:
                        stats["negative_fills"] += 1
                    else:
                        stats["zero_fills"] += 1
                    event = {
                        "decision_ns": int(row["decision_ns"]),
                        "decision_id": str(row["decision_id"]),
                        "market_id": str(row["market_id"]),
                        "asset": str(row.get("asset") or "UNKNOWN"),
                        "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
                        "cash_pnl": pnl,
                        "filled": fill,
                    }
                    stats["events"].append(event)
                    by_asset[event["asset"]][key] += pnl
                    by_horizon[event["contract_horizon"]][key] += pnl
            cells[key] = finish_stats(stats)
            equity[key] = sorted(stats["events"], key=lambda item: (item["decision_ns"], item["decision_id"]))
    return {
        "latencies_ms": list(LATENCIES),
        "exit_horizons_ms": list(EXITS),
        "target_size_shares": SIZE,
        "cells": cells,
        "equity_events": equity,
        "by_asset_pnl": {k: dict(v) for k, v in sorted(by_asset.items())},
        "by_contract_horizon_pnl": {k: dict(v) for k, v in sorted(by_horizon.items())},
    }


def execute_side_cell(
    row: dict[str, Any],
    session: dict[str, Any],
    latency_ms: int,
    exit_ms: int,
    side: str,
) -> tuple[dict[str, Any] | None, str]:
    """Exact baseline execution kernel with side made explicit for research."""
    decision = da.decision_side_state(row, side)
    if decision is None:
        return None, "SIDE_DECISION_EVIDENCE_UNAVAILABLE"
    ask0 = float(decision["ask"])
    depth0 = float(decision["ask_quantity"])
    if float(row["minimum"]) > SIZE + 1e-12:
        return None, "VENUE_MINIMUM_ABOVE_TARGET_SIZE"
    if SIZE > depth0 + 1e-12 or SIZE * ask0 > da.DEFAULT_HARD_ORDER_NOTIONAL + 1e-9 or ask0 > .99:
        return None, "SIZE_DEPTH_OR_NOTIONAL_CAP"
    origin_ms = int(row["decision_ns"]) / 1_000_000.0
    arrival_ms = origin_ms + int(latency_ms)
    exit_target_ms = origin_ms + int(exit_ms)
    watermark = session.get("watermark_ms", session.get("last_wall_ms") or 0)
    if watermark < exit_target_ms:
        return None, "SESSION_WATERMARK_BEFORE_EXIT"
    arrival_pair = pair_asof_session(session, row, arrival_ms)
    exit_pair = pair_asof_session(session, row, exit_target_ms)
    if arrival_pair is None:
        return None, "ARRIVAL_ASOF_UNAVAILABLE"
    if exit_pair is None:
        return None, "EXIT_ASOF_UNAVAILABLE"
    arrival = side_state(arrival_pair, side)
    exit_state = side_state(exit_pair, side)
    if arrival is None:
        return None, "ARRIVAL_L1_DEPTH_UNAVAILABLE"
    if exit_state is None:
        return None, "EXIT_L1_DEPTH_UNAVAILABLE"
    if row.get("epoch") and int(arrival_pair["connection_epoch"]) != int(row["epoch"]):
        return None, "ENTRY_EPOCH_MISMATCH"
    if int(exit_pair["connection_epoch"]) != int(arrival_pair["connection_epoch"]):
        return None, "EXIT_EPOCH_MISMATCH"
    if arrival["ask"] > ask0 + 1e-12:
        return {
            "cash_pnl": 0.0, "filled": 0.0, "exit_filled": 0.0,
            "entry_price": None, "exit_bid": None, "side": side,
        }, "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"
    fill = min(SIZE, arrival["ask_depth"])
    if fill <= 0:
        return {
            "cash_pnl": 0.0, "filled": 0.0, "exit_filled": 0.0,
            "entry_price": None, "exit_bid": None, "side": side,
        }, "OBSERVED_NO_FILL_ZERO_DEPTH"
    exit_fill = min(fill, exit_state["bid_depth"])
    entry_price = float(arrival["ask"])
    exit_bid = float(exit_state["bid"])
    entry_fee = fill * fee_per_share(row, entry_price)
    exit_fee = exit_fill * fee_per_share(row, exit_bid)
    cash = exit_fill * exit_bid - fill * entry_price - entry_fee - exit_fee
    return {
        "cash_pnl": float(cash),
        "filled": float(fill),
        "exit_filled": float(exit_fill),
        "residual_inventory": float(max(0.0, fill - exit_fill)),
        "entry_price": entry_price,
        "exit_bid": exit_bid,
        "side": str(side),
    }, "OBSERVED_FULL_FILL" if fill + 1e-12 >= SIZE else "OBSERVED_PARTIAL_FILL"


def classify_source_feature(name: str) -> str | None:
    n = name.lower()
    if n.startswith("tape.pm_") or any(k in n for k in ("pm_yes_mid", "pm_no_mid", "pm_complete_set", "pm_yes_spread", "pm_yes_imbalance")):
        return "pm_response"
    if any(k in n for k in ("settlement", "oracle", "reference_price", "distance_to_reference", "spot_minus_oracle")):
        return "settlement"
    if any(k in n for k in ("deribit", "atm_iv", "implied_vol", "skew", "term_structure", "option_")):
        return "options"
    if any(k in n for k in ("liquidat", "cascade")):
        return "liquidations"
    if any(k in n for k in ("funding", "open_interest", ".oi", "_oi", "oi_", "oi.")):
        return "oi_funding"
    if any(k in n for k in ("perp", "basis", "mark_price", "index_price")):
        return "perp"
    if any(k in n for k in ("cross_asset", "common_factor", "idiosyncr", "residual_move", "leader_features", "btc_to_", "eth_to_")):
        return "cross_asset"
    if any(k in n for k in ("volatility", "native_vol", "realized_vol", "vol_of_vol", "jump_indicator", "vol_ratio", "vol_fast", "vol_slow")):
        return "volatility"
    if any(k in n for k in ("aggregate_ofi", ".ofi", "_ofi", "microprice", "book_imbalance", "depth_change", "depletion", "book_slope")):
        return "ofi"
    if any(k in n for k in ("trade_imbalance", "signed_volume", "trade_intensity", "volume_accel", "aggressor", "large_trade")):
        return "flow"
    if any(k in n for k in (
        "binance_return", "coinbase_return", "bybit_return", "return_50ms",
        "return_100ms", "return_250ms", "return_1s", "return_2s", "return_5s",
        "dispersion", "agreement", "composite_price", "fresh_venue", "venue_leader",
        "venue_laggard",
    )):
        return "cross_venue"
    if any(k in n for k in ("signal_return", "signal_age", "parent_shock")):
        return "baseline"
    return None


def core_model_feature_allowed(name: str, families: frozenset[str]) -> bool:
    pm_core = (
        "state.ask", "state.bid", "state.spread", "state.depth",
        "state.minimum", "state.tte_s", "state.price_distance_from_half",
        "action.side_sign", "action.size", "action.size2", "action.log_size",
        "action.depth_fraction", "action.notional", "action.notional_fraction_of_cap",
        "action.exit_horizon_ms", "action.log_exit_horizon",
        "system.latency_ms", "system.log_latency",
    )
    if name in pm_core:
        return True
    if name.startswith(("asset::", "contract::", "asset_contract::", "exit::", "latency::")):
        return True
    if name.startswith("x."):
        family = classify_source_feature(name[2:])
        if family == "pm_response":
            return True
        return family in families
    if name in ("state.signal_age_ms", "state.direction", "action.signal_alignment"):
        return "baseline" in families
    if name.startswith("age::") or "effective_action_age" in name:
        return "baseline" in families
    if "signal" in name and name.startswith("interaction."):
        return "baseline" in families
    if name.startswith("state.cross_venue_") or name.startswith("state.return_") or name.startswith("state.trend_"):
        return "cross_venue" in families
    if name.startswith(("action.continuation_", "action.reversal_")):
        return "cross_venue" in families
    if name in ("state.native_vol_fast", "state.native_vol_slow", "state.native_vol_ratio"):
        return "volatility" in families
    if name == "state.external_dispersion_bps" or name == "state.fresh_venues":
        return "cross_venue" in families
    if name == "action.price_extension":
        return True
    if name.startswith(("asset_contract_signal::", "asset_contract_age::")):
        return "baseline" in families
    if name.startswith("asset_contract_size::"):
        return True
    return False


class InformationModel(da.DirectActionValueModel):
    """Direct-action model restricted to a declared information family set."""

    def __init__(self, *, families: Iterable[str], **kwargs):
        self.information_families = frozenset(families)
        super().__init__(**kwargs)

    def _base_feature_names(self, rows):
        available = feature_names(rows)
        selected = []
        for name in available:
            family = classify_source_feature(name)
            if family == "pm_response" or family in self.information_families:
                selected.append(name)
        # 2H screen stays deliberately small.
        return tuple(selected[:96])

    def _configure_levels(self, rows):
        super()._configure_levels(rows)
        self.model_feature_names = tuple(
            name for name in self.model_feature_names
            if core_model_feature_allowed(name, self.information_families)
        )
        if not self.model_feature_names:
            raise ValueError("NO_MODEL_FEATURES_FOR_INFORMATION_SET")


def family_inventory(rows: list[dict[str, Any]]) -> dict[str, Any]:
    names = sorted(feature_names(rows))
    by_family: dict[str, list[str]] = defaultdict(list)
    for name in names:
        family = classify_source_feature(name)
        if family is not None:
            by_family[family].append(name)
    return {
        "numeric_feature_count": len(names),
        "all_numeric_features": names,
        "families": {k: v for k, v in sorted(by_family.items())},
    }


def fit_information_model(
    rows: list[dict[str, Any]],
    families: frozenset[str],
) -> tuple[InformationModel | None, dict[str, Any]]:
    started = time.perf_counter()
    try:
        model = InformationModel(
            families=families,
            size_grid=(SIZE,),
            action_horizons_ms=EXITS,
            train_latencies_ms=LATENCIES,
            max_sizes_per_state=1,
            selection_calibration_mode="OFF",
            support_policy_mode="DIAGNOSTIC",
            conditional_calibration=False,
            streaming_batch_size=2048,
            ridge=8.0,
        ).fit(rows)
    except (ValueError, RuntimeError, ArithmeticError) as exc:
        return None, {
            "state": "INSUFFICIENT_DATA",
            "reason": type(exc).__name__ + ":" + str(exc),
            "fit_seconds": time.perf_counter() - started,
        }
    return model, {
        "state": "READY",
        "fit_seconds": time.perf_counter() - started,
        "families": sorted(families),
        "model_feature_names": list(model.model_feature_names),
        "training_receipt": model.training_receipt,
    }


def predicted_side(
    model: InformationModel,
    row: dict[str, Any],
    *,
    latency_ms: int,
    horizon_ms: int,
) -> tuple[str | None, float, dict[str, float]]:
    values = {}
    for side in da.decision_action_sides(row):
        state = da.decision_side_state(row, side)
        if state is None:
            continue
        if SIZE + 1e-12 < float(row["minimum"]):
            continue
        if SIZE > float(state["ask_quantity"]) + 1e-12:
            continue
        if SIZE * float(state["ask"]) > da.DEFAULT_HARD_ORDER_NOTIONAL + 1e-9:
            continue
        try:
            record = model._action_record(
                row, size=SIZE, horizon_ms=horizon_ms,
                latency_ms=latency_ms, side=side)
            value = float(model.mean_model.predict(record))
        except (ValueError, KeyError, ArithmeticError):
            continue
        if finite(value):
            values[str(side)] = value
    if not values:
        return None, 0.0, values
    side, value = max(values.items(), key=lambda item: (item[1], item[0]))
    if value <= 0:
        return None, float(value), values
    return side, float(value), values


def evaluate_model_cell(
    model: InformationModel,
    rows: list[dict[str, Any]],
    session_cache: dict[str, tuple[dict[str, Any] | None, str | None]],
    *,
    latency_ms: int,
    horizon_ms: int,
) -> dict[str, Any]:
    stats = {
        "opportunities": len(rows), "observed_actions": 0, "trades": 0,
        "fills": 0, "censored": 0, "no_fills": 0, "total_pnl": 0.0,
        "positive_fills": 0, "negative_fills": 0, "zero_fills": 0,
        "events": [], "censoring_reasons": Counter(),
    }
    paired_baseline = 0.0
    paired_model = 0.0
    paired = 0
    prevented = 0
    new_trades = 0
    side_flips = 0
    continuation = 0
    reversal = 0
    decisions = []
    for row in rows:
        session, reason = session_cache[str(row["decision_id"])]
        if session is None:
            stats["censored"] += 1
            stats["censoring_reasons"][str(reason)] += 1
            continue
        baseline_econ, baseline_state = execute_cell(row, session, latency_ms, horizon_ms)
        if baseline_econ is None:
            stats["censored"] += 1
            stats["censoring_reasons"]["BASELINE_" + str(baseline_state)] += 1
            continue
        side, prediction, alternatives = predicted_side(
            model, row, latency_ms=latency_ms, horizon_ms=horizon_ms)
        baseline_side = da.selected_action_side(row)
        if side is None:
            model_econ = {"cash_pnl": 0.0, "filled": 0.0}
            model_state = "MODEL_NO_TRADE"
            prevented += int(float(baseline_econ["cash_pnl"]) < 0)
        else:
            model_econ, model_state = execute_side_cell(
                row, session, latency_ms, horizon_ms, side)
            if model_econ is None:
                stats["censored"] += 1
                stats["censoring_reasons"]["MODEL_" + str(model_state)] += 1
                continue
            stats["trades"] += 1
            if side != baseline_side:
                side_flips += 1
            sign = da.action_side_sign(row, side)
            direction = int(row.get("direction") or 0)
            if sign * direction >= 0:
                continuation += 1
            else:
                reversal += 1
            if float(baseline_econ.get("filled") or 0.0) <= 0 and float(model_econ.get("filled") or 0.0) > 0:
                new_trades += 1
        stats["observed_actions"] += 1
        pnl = float(model_econ["cash_pnl"])
        stats["total_pnl"] += pnl
        fill = float(model_econ.get("filled") or 0.0)
        if side is not None and fill <= 0:
            stats["no_fills"] += 1
        if fill > 0:
            stats["fills"] += 1
            if pnl > 1e-15:
                stats["positive_fills"] += 1
            elif pnl < -1e-15:
                stats["negative_fills"] += 1
            else:
                stats["zero_fills"] += 1
            stats["events"].append({
                "decision_ns": int(row["decision_ns"]),
                "decision_id": str(row["decision_id"]),
                "market_id": str(row["market_id"]),
                "asset": str(row.get("asset") or "UNKNOWN"),
                "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
                "cash_pnl": pnl,
                "side": side,
                "baseline_side": baseline_side,
            })
        paired += 1
        paired_baseline += float(baseline_econ["cash_pnl"])
        paired_model += pnl
        decisions.append({
            "decision_id": str(row["decision_id"]),
            "decision_ns": int(row["decision_ns"]),
            "asset": str(row.get("asset") or "UNKNOWN"),
            "contract_horizon": str(row.get("horizon") or "UNKNOWN"),
            "baseline_side": baseline_side,
            "model_side": side or "NO_TRADE",
            "predicted_pnl": prediction,
            "predicted_by_side": alternatives,
            "baseline_pnl": float(baseline_econ["cash_pnl"]),
            "model_pnl": pnl,
            "model_state": model_state,
        })
    result = finish_stats(stats)
    result.update({
        "paired_opportunities": paired,
        "paired_baseline_total_pnl": paired_baseline if paired else None,
        "paired_model_total_pnl": paired_model if paired else None,
        "delta_pnl": paired_model - paired_baseline if paired else None,
        "bad_baseline_trades_prevented": prevented,
        "new_filled_trades_vs_baseline_no_fill": new_trades,
        "side_flips": side_flips,
        "continuation_actions": continuation,
        "reversal_actions": reversal,
        "decisions": decisions,
        "equity_events": sorted(stats["events"], key=lambda item: (item["decision_ns"], item["decision_id"])),
    })
    return result


def evaluate_model_surface(
    model: InformationModel,
    rows: list[dict[str, Any]],
    session_cache: dict[str, tuple[dict[str, Any] | None, str | None]],
) -> dict[str, Any]:
    cells = {}
    for latency in LATENCIES:
        for horizon in EXITS:
            key = f"{latency}::{horizon}"
            cells[key] = evaluate_model_cell(
                model, rows, session_cache,
                latency_ms=latency, horizon_ms=horizon)
    return {"cells": cells, "latencies_ms": list(LATENCIES), "exit_horizons_ms": list(EXITS)}


def best_cell(surface: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    valid = [
        (key, cell) for key, cell in (surface.get("cells") or {}).items()
        if finite(cell.get("delta_pnl"))
    ]
    if not valid:
        return None, None
    return max(
        valid,
        key=lambda item: (
            float(item[1]["delta_pnl"]),
            float(item[1].get("paired_model_total_pnl") or -1e100),
            int(item[1].get("paired_opportunities") or 0),
            item[0],
        ),
    )


def benchmark_inference(
    model: InformationModel,
    rows: list[dict[str, Any]],
    *,
    latency_ms: int,
    horizon_ms: int,
    repeats: int = 10,
) -> dict[str, Any]:
    samples = rows[: min(200, len(rows))]
    timings = []
    calls = 0
    for _ in range(max(1, repeats)):
        for row in samples:
            start = time.perf_counter_ns()
            predicted_side(model, row, latency_ms=latency_ms, horizon_ms=horizon_ms)
            timings.append(time.perf_counter_ns() - start)
            calls += 1
    return {
        "calls": calls,
        "inference_ns_p50": _quantile(timings, .50),
        "inference_ns_p90": _quantile(timings, .90),
        "inference_ns_p99": _quantile(timings, .99),
        "inference_ns_p999": _quantile(timings, .999),
        "model_path": "OFFLINE_PYTHON_SCREENING_NOT_LIVE_IMPLEMENTATION",
    }


def write_json(path: Path, value: Any) -> None:
    atomic_json(path, value)


def _cumulative_equity(events: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    seq = sorted(events, key=lambda item: (item["decision_ns"], item.get("decision_id", "")))
    x, y = [], []
    total = 0.0
    for row in seq:
        total += float(row["cash_pnl"])
        x.append(int(row["decision_ns"]) / 1e9)
        y.append(total)
    return x, y


def write_figures(
    output: Path,
    *,
    baseline: dict[str, Any],
    validation_results: dict[str, Any],
    test_results: dict[str, Any],
    shortlist: list[dict[str, Any]],
    scorecard: list[dict[str, Any]],
    dynamic_exit: dict[str, Any],
) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception:
        return []
    directory = output / "figures"
    directory.mkdir(parents=True, exist_ok=True)
    written = []

    def save(name):
        path = directory / name
        plt.tight_layout()
        plt.savefig(path, dpi=140)
        plt.close()
        written.append(str(path.relative_to(output)))

    # Baseline reference cell: best total PnL is descriptive only; window was
    # already frozen without PnL.
    base_cells = baseline["cells"]
    base_key = max(
        base_cells,
        key=lambda key: (
            -1e100 if base_cells[key]["total_pnl"] is None
            else float(base_cells[key]["total_pnl"])
        ),
    )
    bx, by = _cumulative_equity(baseline["equity_events"].get(base_key, []))
    plt.figure(figsize=(9, 4))
    plt.plot(bx, by)
    plt.title("Baseline 2H equity — descriptive best grid cell")
    plt.xlabel("Unix time")
    plt.ylabel("Net executable PnL")
    save("01_baseline_2h_equity.png")

    best_name = shortlist[0]["model"] if shortlist else None
    if best_name and best_name in test_results:
        bkey = shortlist[0]["validation_best_cell"]
        events = test_results[best_name]["cells"][bkey]["equity_events"]
        x, y = _cumulative_equity(events)
        plt.figure(figsize=(9, 4)); plt.plot(x, y)
        plt.title("Best enriched model — 2H internal local-test equity")
        plt.xlabel("Unix time"); plt.ylabel("Net executable PnL")
        save("02_best_enriched_equity.png")

        baseline_events = baseline["equity_events"].get(bkey, [])
        bx2, by2 = _cumulative_equity(baseline_events)
        plt.figure(figsize=(9, 4))
        plt.plot(bx2, by2, label="Baseline")
        plt.plot(x, y, label=best_name)
        plt.legend()
        plt.title("Baseline vs enriched equity")
        plt.xlabel("Unix time"); plt.ylabel("Net executable PnL")
        save("03_baseline_vs_enriched_equity.png")

    def heatmap(cells, value, title, name):
        matrix = np.full((len(LATENCIES), len(EXITS)), np.nan)
        for i, latency in enumerate(LATENCIES):
            for j, horizon in enumerate(EXITS):
                v = cells.get(f"{latency}::{horizon}", {}).get(value)
                if finite(v):
                    matrix[i, j] = float(v)
        plt.figure(figsize=(11, 5))
        plt.imshow(matrix, aspect="auto")
        plt.xticks(range(len(EXITS)), EXITS, rotation=45)
        plt.yticks(range(len(LATENCIES)), LATENCIES)
        plt.xlabel("Exit horizon ms"); plt.ylabel("Entry latency ms")
        plt.title(title); plt.colorbar()
        save(name)

    heatmap(base_cells, "total_pnl", "Baseline entry × exit PnL", "04_baseline_entry_exit_heatmap.png")
    if best_name and best_name in test_results:
        cells = test_results[best_name]["cells"]
        heatmap(cells, "paired_model_total_pnl", "Rich model entry × exit PnL", "05_rich_entry_exit_heatmap.png")
        heatmap(cells, "delta_pnl", "Rich minus baseline paired PnL", "06_incremental_heatmap.png")

    if best_name and best_name in validation_results:
        cell = shortlist[0]["validation_best_cell"]
        latency = int(cell.split("::")[0])
        horizons, values = [], []
        for horizon in EXITS:
            c = test_results[best_name]["cells"][f"{latency}::{horizon}"]
            horizons.append(horizon); values.append(c.get("paired_model_total_pnl"))
        plt.figure(figsize=(8, 4)); plt.plot(horizons, values, marker="o")
        plt.title("Signal decay / executable PnL by exit horizon")
        plt.xlabel("Exit horizon ms"); plt.ylabel("PnL")
        save("08_signal_decay.png")

        cont = sum(test_results[best_name]["cells"][cell].get("continuation_actions", 0) for _ in (0,))
        rev = sum(test_results[best_name]["cells"][cell].get("reversal_actions", 0) for _ in (0,))
        plt.figure(figsize=(6, 4)); plt.bar(["Continuation", "Reversal"], [cont, rev])
        plt.title("Momentum / reversal action map")
        save("09_momentum_reversal.png")

    labels = [row["feature"] for row in scorecard if finite(row.get("delta_pnl_2h_internal_test"))]
    vals = [row["delta_pnl_2h_internal_test"] for row in scorecard if finite(row.get("delta_pnl_2h_internal_test"))]
    if labels:
        plt.figure(figsize=(10, 5)); plt.bar(range(len(labels)), vals)
        plt.xticks(range(len(labels)), labels, rotation=45, ha="right")
        plt.title("Incremental PnL contribution by alpha family")
        save("10_alpha_family_contribution.png")

    dyn = dynamic_exit.get("diagnostic") or {}
    decisions = dyn.get("decisions") or {}
    if decisions:
        keys = list(decisions)
        plt.figure(figsize=(8, 4)); plt.bar(range(len(keys)), [decisions[k] for k in keys])
        plt.xticks(range(len(keys)), keys, rotation=45, ha="right")
        plt.title("Dynamic exit decisions")
        save("11_dynamic_exit.png")

    if best_name and shortlist:
        cell = shortlist[0]["validation_best_cell"]
        events = test_results[best_name]["cells"][cell]["equity_events"]
        asset = defaultdict(float); contract = defaultdict(float)
        for event in events:
            asset[event["asset"]] += float(event["cash_pnl"])
            contract[event["contract_horizon"]] += float(event["cash_pnl"])
        if asset:
            keys = sorted(asset)
            plt.figure(figsize=(8, 4)); plt.bar(range(len(keys)), [asset[k] for k in keys])
            plt.xticks(range(len(keys)), keys, rotation=45); plt.title("PnL by asset")
            save("12_pnl_by_asset.png")
        if contract:
            keys = sorted(contract)
            plt.figure(figsize=(8, 4)); plt.bar(range(len(keys)), [contract[k] for k in keys])
            plt.xticks(range(len(keys)), keys, rotation=45); plt.title("PnL by contract horizon")
            save("13_pnl_by_contract_horizon.png")

    # Information-latency figure is written by the caller if frontier exists.
    return written


def run_program(
    *,
    root: Path,
    output: Path,
    code_sha: str,
    minimum_wall_ns: int,
    feature_tapes: Iterable[Path] = (),
) -> dict[str, Any]:
    if len(code_sha) != 40 or any(ch not in "0123456789abcdef" for ch in code_sha):
        raise ValueError("exact 40-hex code SHA required")
    output.mkdir(parents=True, exist_ok=False)

    data = build_dataset(
        root,
        minimum_wall_ns=minimum_wall_ns,
        include_settlement_labels=False,
        use_compact_window_index=True,
    )
    if data.get("input_state") != "READY":
        raise ValueError("CAUSAL_DATASET_NOT_READY:" + str(data.get("input_state")))
    source_rows = [row for row in data["decisions"] if da._valid_state(row)]
    sessions, tape_diag = stream_sessions(Path(root).resolve().parent, source_rows)
    if not sessions:
        sessions, fallback = jsonl_sessions(Path(root).resolve(), source_rows)
        tape_diag = {**tape_diag, **fallback, "fallback": "JSONL_BOOK_OBSERVATIONS"}
    market_index = build_market_session_index(sessions)
    session_cache = {
        str(row["decision_id"]): resolve_session(row, market_index)
        for row in source_rows
    }

    window = select_two_hour_window(source_rows, session_cache)
    start_ns, end_ns = int(window["start_ns"]), int(window["end_ns"])
    rows = [
        row for row in source_rows
        if start_ns <= int(row["decision_ns"]) < end_ns
    ]
    splits = split_rows(rows, start_ns)
    if not all(splits.values()):
        raise ValueError("TWO_HOUR_CHRONOLOGICAL_SPLIT_EMPTY")

    paths = list(feature_tapes) or discover_feature_tapes(Path(root))
    feature_index, feature_tape_diag = load_feature_tape(
        paths, start_ns=start_ns, end_ns=end_ns)
    rich_rows, join_diag = attach_rich_state(rows, feature_index, delay_ms=0)
    rich_by_id = {str(row["decision_id"]): row for row in rich_rows}
    rich_splits = {
        name: [rich_by_id[str(row["decision_id"])] for row in seq]
        for name, seq in splits.items()
    }
    inventory = family_inventory(rich_rows)

    baseline = baseline_surface(rows, session_cache)
    manifest = {
        "schema": SCHEMA + "_manifest",
        **SAFETY_PLUS,
        "window_start_ns": start_ns,
        "window_end_ns": end_ns,
        "window_duration_seconds": 7200,
        "selection": window,
        "code_sha": code_sha,
        "data_sha256": data.get("data_sha256"),
        "source_evidence": data.get("sources"),
        "continuous_pm_tape": tape_diag,
        "feature_tape": feature_tape_diag,
        "chronological_split": {
            name: len(seq) for name, seq in splits.items()
        },
        "purpose": "RESEARCH_DEVELOPMENT_ALPHA_DISCOVERY_NOT_FINAL_OOS",
    }
    baseline_manifest = {
        "schema": SCHEMA + "_baseline_manifest",
        **SAFETY_PLUS,
        "baseline_name": "BASELINE_V0",
        "baseline_origin_commit": BASELINE_ORIGIN_COMMIT,
        "baseline_code_sha": code_sha,
        "baseline_module": BASELINE_MODULE,
        "execution_kernel_module": BASELINE_KERNEL_MODULE,
        "baseline_data_sha": data.get("data_sha256"),
        "baseline_strategy_definition": (
            "CURRENT_ALL_CRYPTO_CONTINUOUS_PM_L1_TIMING_EQUITY;"
            "SELECTED_BASELINE_SIDE;ZERO_CHASE_LIMIT;EXECUTABLE_ASK_ENTRY;"
            "EXECUTABLE_BID_EXIT;L1_DEPTH;FEES;CAUSAL_BACKWARD_ASOF;"
            "MISSING_EVIDENCE_CENSORED"
        ),
        "latencies_ms": list(LATENCIES),
        "exit_horizons_ms": list(EXITS),
        "size_shares": SIZE,
        "fees": "CANONICAL fee_per_share FROM WALK_FORWARD_V2",
        "depth_semantics": "OBSERVED_L1_ONLY_NO_IMPUTATION",
        "censoring_semantics": "MISSING_OR_DISCONTINUOUS_EVIDENCE_IS_NA_NOT_ZERO",
        "assets": sorted({str(row.get("asset") or "UNKNOWN") for row in rows}),
        "contract_horizons": sorted({str(row.get("horizon") or "UNKNOWN") for row in rows}),
    }

    validation_results = {}
    test_results = {}
    model_receipts = {}
    models = {}
    for name, families in NESTED.items():
        model, receipt = fit_information_model(rich_splits["TRAIN_60"], families)
        model_receipts[name] = receipt
        if model is None:
            continue
        models[name] = model
        validation_results[name] = evaluate_model_surface(
            model, rich_splits["VALIDATION_20"], session_cache)
        test_results[name] = evaluate_model_surface(
            model, rich_splits["LOCAL_TEST_20"], session_cache)

    family_models = {}
    family_results = {}
    family_receipts = {}
    for family in FAMILY_ORDER[1:] + ("pm_response",):
        families = frozenset(("baseline", family))
        model, receipt = fit_information_model(rich_splits["TRAIN_60"], families)
        family_receipts[family] = receipt
        if model is None:
            continue
        family_models[family] = model
        validation = evaluate_model_surface(
            model, rich_splits["VALIDATION_20"], session_cache)
        test = evaluate_model_surface(
            model, rich_splits["LOCAL_TEST_20"], session_cache)
        family_results[family] = {"validation": validation, "test": test}

    candidate_rows = []
    for name, surface in validation_results.items():
        key, cell = best_cell(surface)
        if key is None:
            continue
        test_cell = test_results[name]["cells"].get(key)
        candidate_rows.append({
            "model": name,
            "families": sorted(NESTED[name]),
            "validation_best_cell": key,
            "validation_delta_pnl": cell.get("delta_pnl"),
            "validation_model_pnl": cell.get("paired_model_total_pnl"),
            "internal_test_delta_pnl": test_cell.get("delta_pnl") if test_cell else None,
            "internal_test_model_pnl": test_cell.get("paired_model_total_pnl") if test_cell else None,
            "paired_test_opportunities": test_cell.get("paired_opportunities") if test_cell else 0,
        })
    candidate_rows.sort(
        key=lambda row: (
            -1e100 if row["internal_test_delta_pnl"] is None else float(row["internal_test_delta_pnl"]),
            -1e100 if row["validation_delta_pnl"] is None else float(row["validation_delta_pnl"]),
        ),
        reverse=True,
    )
    shortlist = candidate_rows[:4]

    # Latency-delay frontier for strongest internally selected model.
    info_latency = {
        "schema": SCHEMA + "_information_latency",
        **SAFETY_PLUS,
        "delays_ms": list(DELAY_GRID_MS),
        "models": {},
    }
    if shortlist:
        top = shortlist[0]
        name = top["model"]
        model = models[name]
        cell_key = top["validation_best_cell"]
        latency, horizon = (int(v) for v in cell_key.split("::"))
        baseline_at_cell = baseline["cells"].get(cell_key) or {}
        fresh_value = None
        for delay in DELAY_GRID_MS:
            delayed, delay_diag = attach_rich_state(
                splits["LOCAL_TEST_20"], feature_index, delay_ms=delay)
            result = evaluate_model_cell(
                model, delayed, session_cache,
                latency_ms=latency, horizon_ms=horizon)
            value = result.get("paired_model_total_pnl")
            if delay == 0:
                fresh_value = value
            info_latency["models"].setdefault(name, {})[str(delay)] = {
                "result": result,
                "join": delay_diag,
                "information_gain_vs_baseline": (
                    None if value is None or result.get("paired_baseline_total_pnl") is None
                    else float(value) - float(result["paired_baseline_total_pnl"])
                ),
                "latency_cost_vs_fresh": (
                    None if fresh_value is None or value is None
                    else float(fresh_value) - float(value)
                ),
                "net_information_value": result.get("delta_pnl"),
            }
        info_latency["selected_cell"] = {
            "model": name, "latency_ms": latency, "exit_horizon_ms": horizon,
            "baseline_full_2h_reference": baseline_at_cell,
        }

    scorecard = []
    baseline_model_validation = validation_results.get("F1")
    baseline_model_test = test_results.get("F1")
    baseline_model_key, _ = best_cell(baseline_model_validation or {})
    for family in (
        "cross_venue", "flow", "ofi", "perp", "oi_funding", "liquidations",
        "volatility", "cross_asset", "options", "pm_response", "settlement",
    ):
        item = {
            "feature": FAMILY_LABEL.get(family, family),
            "family": family,
            "available_features": inventory["families"].get(family, []),
            "delta_pnl_2h_internal_test": None,
            "delta_pnl_per_trade": None,
            "robustness": "UNASSESSED_LONG_WINDOW",
            "delay_decay": None,
            "compute_cost": None,
            "status": "INSUFFICIENT_DATA",
        }
        result = family_results.get(family)
        if result:
            key, val_cell = best_cell(result["validation"])
            if key is not None:
                test_cell = result["test"]["cells"][key]
                item["validation_best_cell"] = key
                item["delta_pnl_2h_internal_test"] = test_cell.get("delta_pnl")
                trades = int(test_cell.get("trades") or 0)
                item["delta_pnl_per_trade"] = (
                    float(test_cell["delta_pnl"]) / trades
                    if trades and finite(test_cell.get("delta_pnl")) else None
                )
                delta = test_cell.get("delta_pnl")
                neighboring = []
                l, h = (int(v) for v in key.split("::"))
                li = list(LATENCIES).index(l)
                hi = list(EXITS).index(h)
                for ii, jj in ((li-1,hi),(li+1,hi),(li,hi-1),(li,hi+1)):
                    if 0 <= ii < len(LATENCIES) and 0 <= jj < len(EXITS):
                        neighbor = result["test"]["cells"][f"{LATENCIES[ii]}::{EXITS[jj]}"].get("delta_pnl")
                        if finite(neighbor):
                            neighboring.append(float(neighbor))
                positive_neighbors = sum(v > 0 for v in neighboring)
                item["robustness"] = {
                    "neighbor_cells_observed": len(neighboring),
                    "positive_neighbor_cells": positive_neighbors,
                }
                if not inventory["families"].get(family):
                    item["status"] = "INSUFFICIENT_DATA"
                elif finite(delta) and float(delta) > 0 and positive_neighbors >= max(1, len(neighboring)//2):
                    item["status"] = "PROMISING_2H_FAST" if family in ("cross_venue","flow","ofi","pm_response") else "PROMISING_2H_ASYNC"
                else:
                    item["status"] = "REJECT_2H_SCREEN"
        scorecard.append(item)

    rejected = [
        {
            "family": row["family"],
            "reason": (
                "NO_USABLE_CAUSAL_FEATURE_EVIDENCE"
                if row["status"] == "INSUFFICIENT_DATA"
                else "NO_ROBUST_POSITIVE_INCREMENTAL_PNL_IN_2H_SCREEN"
            ),
        }
        for row in scorecard
        if row["status"] in ("REJECT_2H_SCREEN", "INSUFFICIENT_DATA")
    ]

    dynamic = {
        "schema": SCHEMA + "_dynamic_exit",
        **SAFETY_PLUS,
        "state": "INSUFFICIENT_DATA",
    }
    try:
        dynamic_model = DynamicExitValueModel().fit(rich_splits["TRAIN_60"])
        dynamic = {
            "schema": SCHEMA + "_dynamic_exit",
            **SAFETY_PLUS,
            "state": "READY",
            "training_receipt": dynamic_model.training_receipt,
            "diagnostic": summarize_dynamic_exit(
                dynamic_model, rich_splits["LOCAL_TEST_20"], position_size=SIZE),
            "interpretation": (
                "2H_INTERNAL_RESEARCH_HOLD_VS_EXIT_DIAGNOSTIC;"
                "NOT_AUTOMATIC_POLICY_PROMOTION"
            ),
        }
    except (ValueError, RuntimeError) as exc:
        dynamic["reason"] = type(exc).__name__ + ":" + str(exc)

    feature_costs = {
        "schema": SCHEMA + "_feature_costs",
        **SAFETY_PLUS,
        "causal_join": join_diag,
        "models": {},
        "live_path_claim": (
            "NO_LIVE_LATENCY_CLAIM_FROM_OFFLINE_PYTHON;"
            "RICH_STATE_MUST_REMAIN_PRECOMPUTED_OFF_PATH"
        ),
    }
    for row in shortlist:
        name = row["model"]
        latency, horizon = (int(v) for v in row["validation_best_cell"].split("::"))
        feature_costs["models"][name] = benchmark_inference(
            models[name], rich_splits["LOCAL_TEST_20"],
            latency_ms=latency, horizon_ms=horizon)

    nested_artifact = {
        "schema": SCHEMA + "_nested_models",
        **SAFETY_PLUS,
        "nested_information_sets": {k: sorted(v) for k, v in NESTED.items()},
        "training_receipts": model_receipts,
        "validation": validation_results,
        "internal_test": test_results,
    }
    family_artifact = {
        "schema": SCHEMA + "_univariate_family_additions",
        **SAFETY_PLUS,
        "base_information": ["baseline"],
        "family_training_receipts": family_receipts,
        "results": family_results,
    }
    cube = {
        "schema": SCHEMA + "_information_entry_exit_cube",
        **SAFETY_PLUS,
        "validation": {
            name: {
                key: {
                    k: v for k, v in cell.items()
                    if k not in ("decisions", "equity_events")
                }
                for key, cell in surface["cells"].items()
            }
            for name, surface in validation_results.items()
        },
        "internal_test": {
            name: {
                key: {
                    k: v for k, v in cell.items()
                    if k not in ("decisions", "equity_events")
                }
                for key, cell in surface["cells"].items()
            }
            for name, surface in test_results.items()
        },
    }
    signal_decay = {}
    momentum_reversal = {}
    equities = {"baseline": baseline["equity_events"], "candidates": {}}
    for candidate in shortlist:
        name = candidate["model"]
        key = candidate["validation_best_cell"]
        latency = int(key.split("::")[0])
        signal_decay[name] = {
            str(h): {
                k: v for k, v in test_results[name]["cells"][f"{latency}::{h}"].items()
                if k not in ("decisions", "equity_events")
            }
            for h in EXITS
        }
        cell = test_results[name]["cells"][key]
        momentum_reversal[name] = {
            "cell": key,
            "side_flips": cell["side_flips"],
            "continuation_actions": cell["continuation_actions"],
            "reversal_actions": cell["reversal_actions"],
            "bad_baseline_trades_prevented": cell["bad_baseline_trades_prevented"],
            "new_filled_trades_vs_baseline_no_fill": cell["new_filled_trades_vs_baseline_no_fill"],
        }
        equities["candidates"][name] = {
            "cell": key,
            "events": cell["equity_events"],
        }

    coverage = {
        "schema": SCHEMA + "_data_coverage",
        **SAFETY_PLUS,
        "rows": len(rows),
        "assets": sorted({str(row.get("asset") or "UNKNOWN") for row in rows}),
        "contract_horizons": sorted({str(row.get("horizon") or "UNKNOWN") for row in rows}),
        "continuous_pm_rows": sum(session_cache[str(row["decision_id"])][0] is not None for row in rows),
        "continuous_pm_rate": (
            sum(session_cache[str(row["decision_id"])][0] is not None for row in rows) / len(rows)
            if rows else None
        ),
        "feature_join": join_diag,
        "feature_inventory": inventory,
        "gaps_and_censoring": {
            key: value["censoring_reasons"] for key, value in baseline["cells"].items()
        },
    }
    external_backfill = {
        "schema": SCHEMA + "_external_backfill",
        **SAFETY_PLUS,
        "causal_feature_tape_files": feature_tape_diag["files"],
        "causal_feature_tape_records": feature_tape_diag["counts"],
        "historical_public_backfill_policy": (
            "USE_PUBLIC_HISTORY_FOR_RECONSTRUCTIBLE_SLOW_OR_EVENT_TIME_FEATURES;"
            "DO_NOT_SUBSTITUTE_EXCHANGE_EVENT_TIME_FOR_LONDON_RECEIVE_TIME_AT_5_10_25MS"
        ),
        "tier1_state": (
            "READY_FROM_CAUSAL_FEATURE_TAPE"
            if feature_tape_diag["counts"].get("accepted", 0) else
            "INSUFFICIENT_CAUSAL_RICH_STATE"
        ),
    }
    causal_join = {
        "schema": SCHEMA + "_causal_join",
        **SAFETY_PLUS,
        "join": join_diag,
        "rules": {
            "feature_ready_time": "available_at_ns <= decision_ns - artificial_delay",
            "pm_execution_state": "last valid receive-time state <= target time",
            "future_nearest_join": False,
            "interpolation_from_future": False,
            "missing_to_zero": False,
        },
    }

    write_json(output / "01_2h_manifest.json", manifest)
    write_json(output / "02_data_coverage.json", coverage)
    write_json(output / "03_baseline_manifest.json", baseline_manifest)
    write_json(output / "04_baseline_2h.json", {
        "schema": SCHEMA + "_baseline_2h", **SAFETY_PLUS, **baseline})
    write_json(output / "05_external_backfill.json", external_backfill)
    write_json(output / "06_causal_join.json", causal_join)
    write_json(output / "07_univariate_alpha.json", family_artifact)
    write_json(output / "08_nested_models.json", nested_artifact)
    write_json(output / "09_information_latency.json", info_latency)
    write_json(output / "10_entry_exit_information_cube.json", cube)
    write_json(output / "11_signal_decay.json", {
        "schema": SCHEMA + "_signal_decay", **SAFETY_PLUS, "models": signal_decay})
    write_json(output / "12_momentum_reversal.json", {
        "schema": SCHEMA + "_momentum_reversal", **SAFETY_PLUS, "models": momentum_reversal})
    write_json(output / "13_dynamic_exit.json", dynamic)
    write_json(output / "14_feature_costs.json", feature_costs)
    write_json(output / "15_equities.json", {
        "schema": SCHEMA + "_equities", **SAFETY_PLUS, **equities})
    write_json(output / "16_feature_scorecard.json", {
        "schema": SCHEMA + "_feature_scorecard", **SAFETY_PLUS, "rows": scorecard})
    write_json(output / "17_shortlist.json", {
        "schema": SCHEMA + "_shortlist", **SAFETY_PLUS,
        "maximum_candidates": 4, "candidates": shortlist,
        "promotion": "NONE_RESEARCH_ONLY"})
    write_json(output / "18_rejected_features.json", {
        "schema": SCHEMA + "_rejected_features", **SAFETY_PLUS, "features": rejected})

    next_stage = """# Next stage

This run is a 2H internal research screen. It is not final statistical evidence.

1. Freeze at most C0 baseline plus the strongest three challengers from 17_shortlist.json.
2. Re-run only those candidates on a materially longer historical interval.
3. Do not re-open the full feature/model grid during the robustness stage.
4. Freeze feature set, model, hyperparameters, latency assumptions, size, execution and exit.
5. Collect untouched future PAPER evidence.
6. No tuning on the frozen future PAPER period.
7. Keep pure-arbitrage and statistical-alpha execution paths isolated.
"""
    (output / "19_next_stage_plan.md").write_text(next_stage, encoding="utf-8")

    figures = write_figures(
        output,
        baseline=baseline,
        validation_results=validation_results,
        test_results=test_results,
        shortlist=shortlist,
        scorecard=scorecard,
        dynamic_exit=dynamic,
    )

    # Add latency frontier figure after the generic figure pass.
    if shortlist and info_latency.get("models"):
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            name = shortlist[0]["model"]
            points = info_latency["models"][name]
            xs = [int(d) for d in points]
            ys = [
                points[str(d)]["result"].get("paired_model_total_pnl")
                for d in xs
            ]
            plt.figure(figsize=(8, 4))
            plt.plot(xs, ys, marker="o")
            plt.xlabel("Artificial information delay ms")
            plt.ylabel("Executable PnL")
            plt.title("Information-latency frontier")
            plt.tight_layout()
            path = output / "figures" / "07_information_latency_frontier.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(path, dpi=140)
            plt.close()
            figures.append(str(path.relative_to(output)))
        except Exception:
            pass

    summary_lines = [
        "# POLYMARKET 2H MULTI-ALPHA RESEARCH",
        "",
        "**Status:** 2H INTERNAL RESEARCH TEST. Not final OOS. PAPER only.",
        "",
        f"- Window: {start_ns} → {end_ns} (exactly 7200s)",
        f"- Decisions: {len(rows)}",
        f"- Assets: {', '.join(baseline_manifest['assets'])}",
        f"- Contract horizons: {', '.join(baseline_manifest['contract_horizons'])}",
        f"- Continuous PM coverage: {coverage['continuous_pm_rate']:.3f}" if coverage["continuous_pm_rate"] is not None else "- Continuous PM coverage: NA",
        f"- Rich causal feature join: {join_diag['join_rate']:.3f}" if join_diag["join_rate"] is not None else "- Rich causal feature join: NA",
        "",
        "## Shortlist",
    ]
    if shortlist:
        for i, row in enumerate(shortlist):
            summary_lines.append(
                f"- C{i}: {row['model']} @ {row['validation_best_cell']} — "
                f"internal-test ΔPnL={row['internal_test_delta_pnl']}"
            )
    else:
        summary_lines.append("- No challenger had sufficient causal evidence.")
    summary_lines.extend([
        "",
        "## Interpretation guardrail",
        "",
        "The 2H window was chosen before alpha inspection using data quality only. "
        "Repeated research contaminates this sandbox. Promising results must be frozen "
        "and tested on longer untouched data, then on future PAPER evidence.",
        "",
        "Pure arbitrage remains outside this research path.",
    ])
    (output / "00_executive_summary.md").write_text(
        "\n".join(summary_lines) + "\n", encoding="utf-8")

    return {
        "schema": SCHEMA,
        **SAFETY_PLUS,
        "state": "READY",
        "output_directory": str(output),
        "window_start_ns": start_ns,
        "window_end_ns": end_ns,
        "rows": len(rows),
        "shortlist": shortlist,
        "figures": sorted(set(figures)),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--minimum-wall-ns", type=int, required=True)
    parser.add_argument("--feature-tape", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        result = run_program(
            root=args.root,
            output=args.output_dir,
            code_sha=args.code_sha,
            minimum_wall_ns=args.minimum_wall_ns,
            feature_tapes=args.feature_tape,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, type(exc).__name__ + ":" + str(exc) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
