#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import v7_micro_taker_data as base
import v7_micro_taker_core as economics
from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event
from v7_shared_market_state import SharedStateError

FLOW_FEATURE_DIM = 8
DATASET_VERSION = 2
DATASET_LINEAGE = "EVENT_NOVEL_SHARED_STATE_V2"
COMPLETE_ROUND_TRIP_EXECUTION_CONTRACT = "complete_round_trip_executable_ev"
CONSERVATIVE_MARKING_CONTRACT = "full_depth_executable_bid_net_fee_or_zero_fail_closed"
LIVE_FLOW_SCHEMA = "polymarket_v7_live_trade_flow_v1"


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def residual_sigma(samples: list[dict[str, Any]], beta: list[float]) -> float:
    residuals: list[float] = []
    p = len(beta)
    for row in samples[-10000:]:
        if row.get("y") is None:
            continue
        try:
            x = [float(value) for value in row["x"]]
            y = float(row["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(x) != p:
            continue
        prediction = sum(a * b for a, b in zip(beta, x))
        residuals.append(y - prediction)
    if len(residuals) < 20:
        return 0.02
    return max(1e-4, statistics.stdev(residuals))


def sample_key(market_id: str, yes: base.Book, no: base.Book) -> str:
    """Identify one economically observable atomic YES/NO state.

    Snapshot publication is deliberately absent: republishing an unchanged
    atomic cut cannot manufacture an independent training observation.
    """
    return ":".join((
        str(market_id),
        str(yes.lineage_epoch), str(yes.state_version),
        str(no.lineage_epoch), str(no.state_version),
    ))


def sample_diagnostics(samples: list[dict[str, Any]], *, horizon_seconds: int = 30) -> dict[str, Any]:
    labeled = [row for row in samples if row.get("y") is not None]
    targets = [_finite(row.get("y")) for row in labeled]
    targets = [value for value in targets if math.isfinite(value)]
    nonzero = sum(abs(value) > 1e-12 for value in targets)
    target_variance = statistics.pvariance(targets) if len(targets) >= 2 else 0.0
    flow_nonzero = sum(
        isinstance(row.get("x"), list) and len(row["x"]) == FLOW_FEATURE_DIM
        and (abs(_finite(row["x"][-2], 0.0)) > 1e-12
             or abs(_finite(row["x"][-1], 0.0)) > 1e-12)
        for row in samples
    )
    unique_keys = {
        str(row.get("sample_key") or "") for row in samples
        if str(row.get("sample_key") or "")
    }
    unique_markets = {str(row.get("market_id") or "") for row in samples}
    time_bucket_seconds = max(1, int(horizon_seconds))
    independent_time_buckets = {
        int(_finite(row.get("ts"), 0.0)) // time_bucket_seconds for row in labeled
        if int(_finite(row.get("ts"), 0.0)) > 0
    }
    unique_events = {
        str(row.get("event_id") or row.get("market_id") or "") for row in labeled
        if str(row.get("event_id") or row.get("market_id") or "")
    }
    return {
        "raw_samples": len(samples),
        "labeled_samples": len(targets),
        "nonzero_labeled_samples": nonzero,
        "nonzero_label_fraction": nonzero / len(targets) if targets else 0.0,
        "target_variance": target_variance,
        "flow_nonzero_samples": flow_nonzero,
        "flow_nonzero_fraction": flow_nonzero / len(samples) if samples else 0.0,
        "effective_sample_size": len(unique_keys),
        "unique_markets": len(unique_markets - {""}),
        "independent_time_buckets": len(independent_time_buckets),
        "unique_events": len(unique_events),
    }


def model_validity(
    diagnostics: dict[str, Any], *, minimum_samples: int = 200,
    minimum_time_buckets: int = 12,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if int(diagnostics.get("labeled_samples") or 0) < minimum_samples:
        reasons.append("INSUFFICIENT_LABELED_SAMPLES")
    if int(diagnostics.get("effective_sample_size") or 0) < minimum_samples:
        reasons.append("INSUFFICIENT_EFFECTIVE_SAMPLE_SIZE")
    if float(diagnostics.get("target_variance") or 0.0) <= 1e-12:
        reasons.append("DEGENERATE_ZERO_TARGET_VARIANCE")
    if int(diagnostics.get("flow_nonzero_samples") or 0) <= 0:
        reasons.append("NO_CAUSAL_FLOW_COVERAGE")
    if int(diagnostics.get("independent_time_buckets") or 0) < minimum_time_buckets:
        reasons.append("INSUFFICIENT_INDEPENDENT_TIME_BUCKETS")
    return not reasons, reasons


def chronological_oos_diagnostics(
    samples: list[dict[str, Any]], *, horizon_seconds: int,
    minimum_train_samples: int = 120, minimum_oos_samples: int = 40,
) -> dict[str, Any]:
    """Purged chronological holdout proving that alpha exists beyond fit data.

    Many markets update in the same second, so raw row count is not treated as
    independent evidence. The purge removes training labels whose forecast
    horizon overlaps the holdout boundary. Risk remains closed unless the model
    beats a no-change forecast and has positive directional/correlation skill.
    """
    rows: list[dict[str, Any]] = []
    for row in samples:
        try:
            x = [float(value) for value in row["x"]]
            y = float(row["y"])
            ts = int(row["ts"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if len(x) != FLOW_FEATURE_DIM or not all(math.isfinite(value) for value in x + [y]) or ts <= 0:
            continue
        rows.append({**row, "x": x, "y": y, "ts": ts})
    rows.sort(key=lambda row: (int(row["ts"]), str(row.get("sample_key") or "")))
    result: dict[str, Any] = {
        "valid": False, "reasons": [], "train_samples": 0, "oos_samples": 0,
        "purge_seconds": max(1, int(horizon_seconds)), "baseline_mse": None,
        "model_mse": None, "mse_improvement_fraction": None,
        "prediction_target_correlation": None, "directional_accuracy": None,
        "oos_residual_sigma": None,
    }
    if len(rows) < minimum_train_samples + minimum_oos_samples:
        result["reasons"] = ["INSUFFICIENT_CHRONOLOGICAL_OOS_SAMPLES"]
        return result
    split = max(minimum_train_samples, int(len(rows) * 0.80))
    split = min(split, len(rows) - minimum_oos_samples)
    holdout = rows[split:]
    holdout_start = int(holdout[0]["ts"])
    train = [
        row for row in rows[:split]
        if int(row["ts"]) + max(1, int(horizon_seconds)) < holdout_start
    ]
    result["train_samples"] = len(train)
    result["oos_samples"] = len(holdout)
    reasons: list[str] = []
    if len(train) < minimum_train_samples:
        reasons.append("INSUFFICIENT_PURGED_TRAIN_SAMPLES")
    if len(holdout) < minimum_oos_samples:
        reasons.append("INSUFFICIENT_CHRONOLOGICAL_OOS_SAMPLES")
    if reasons:
        result["reasons"] = reasons
        return result
    beta = solve_ridge(train, 1e-2, FLOW_FEATURE_DIM)
    predictions = [sum(a * b for a, b in zip(beta, row["x"])) for row in holdout]
    targets = [float(row["y"]) for row in holdout]
    residuals = [target - prediction for target, prediction in zip(targets, predictions)]
    baseline_mse = statistics.fmean(target * target for target in targets)
    model_mse = statistics.fmean(value * value for value in residuals)
    improvement = (baseline_mse - model_mse) / baseline_mse if baseline_mse > 1e-15 else -math.inf
    pred_mean, target_mean = statistics.fmean(predictions), statistics.fmean(targets)
    covariance = statistics.fmean(
        (prediction - pred_mean) * (target - target_mean)
        for prediction, target in zip(predictions, targets)
    )
    pred_var = statistics.fmean((prediction - pred_mean) ** 2 for prediction in predictions)
    target_var = statistics.fmean((target - target_mean) ** 2 for target in targets)
    correlation = covariance / math.sqrt(pred_var * target_var) if pred_var > 1e-18 and target_var > 1e-18 else 0.0
    active = [
        (prediction, target) for prediction, target in zip(predictions, targets)
        if abs(prediction) > 1e-8 and abs(target) > 1e-12
    ]
    directional_accuracy = (
        sum((prediction > 0.0) == (target > 0.0) for prediction, target in active) / len(active)
        if active else 0.0
    )
    sigma = statistics.stdev(residuals) if len(residuals) >= 2 else math.inf
    if improvement < 0.02:
        reasons.append("OOS_DOES_NOT_BEAT_NO_CHANGE_BASELINE")
    if correlation < 0.05:
        reasons.append("OOS_NONPOSITIVE_PREDICTIVE_CORRELATION")
    if len(active) < minimum_oos_samples // 2 or directional_accuracy < 0.52:
        reasons.append("OOS_DIRECTIONAL_SKILL_NOT_ESTABLISHED")
    result.update({
        "valid": not reasons, "reasons": reasons,
        "baseline_mse": baseline_mse, "model_mse": model_mse,
        "mse_improvement_fraction": improvement,
        "prediction_target_correlation": correlation,
        "directional_accuracy": directional_accuracy,
        "directional_samples": len(active),
        "oos_residual_sigma": max(1e-4, sigma) if math.isfinite(sigma) else None,
    })
    return result


def solve_ridge(samples: list[dict[str, Any]], ridge: float, feature_dim: int) -> list[float]:
    labeled: list[dict[str, Any]] = []
    for row in samples:
        if row.get("y") is None:
            continue
        try:
            x = [float(v) for v in row["x"]]
            float(row["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(x) == feature_dim:
            labeled.append(row)
    if len(labeled) < 40:
        return [0.0] * feature_dim
    p = feature_dim
    matrix = [[0.0] * p for _ in range(p)]
    rhs = [0.0] * p
    for row in labeled[-10000:]:
        x = [float(v) for v in row["x"]]
        target = float(row["y"])
        for i in range(p):
            rhs[i] += x[i] * target
            for j in range(p):
                matrix[i][j] += x[i] * x[j]
    for i in range(1, p):
        matrix[i][i] += ridge
    for i in range(p):
        pivot = max(range(i, p), key=lambda r: abs(matrix[r][i]))
        if abs(matrix[pivot][i]) < 1e-12:
            return [0.0] * p
        matrix[i], matrix[pivot] = matrix[pivot], matrix[i]
        rhs[i], rhs[pivot] = rhs[pivot], rhs[i]
        diagonal = matrix[i][i]
        matrix[i] = [value / diagonal for value in matrix[i]]
        rhs[i] /= diagonal
        for r in range(p):
            if r == i:
                continue
            q = matrix[r][i]
            if abs(q) < 1e-14:
                continue
            matrix[r] = [matrix[r][c] - q * matrix[i][c] for c in range(p)]
            rhs[r] -= q * rhs[i]
    return rhs


def causal_flow_features(
    trade_tape: Path,
    token_ids: set[str],
    *,
    now: int,
    lookback_seconds: int,
    half_life_seconds: float,
) -> dict[str, dict[str, float]]:
    out = {token: {"signed_imbalance": 0.0, "weighted_gross": 0.0, "prints": 0.0} for token in token_ids}
    if not token_ids or not trade_tape.exists():
        return out
    signed: dict[str, float] = {token: 0.0 for token in token_ids}
    gross: dict[str, float] = {token: 0.0 for token in token_ids}
    prints: dict[str, int] = {token: 0 for token in token_ids}
    now_ms = int(now) * 1000
    decay_scale = math.log(2.0) / max(1e-6, float(half_life_seconds))
    try:
        with trade_tape.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                token = str(row.get("asset_id") or "")
                if token not in token_ids:
                    continue
                event_ts = int(_finite(row.get("timestamp"), 0.0))
                received_ms = int(_finite(row.get("received_ms"), 0.0))
                if event_ts <= 0 or received_ms <= 0 or received_ms > now_ms or event_ts > now:
                    continue
                age = now - event_ts
                if age < 0 or age > max(1, int(lookback_seconds)):
                    continue
                size = max(0.0, _finite(row.get("size"), 0.0))
                side = str(row.get("side") or "").upper()
                if size <= 0.0 or side not in {"BUY", "SELL"}:
                    continue
                weight = math.exp(-decay_scale * age)
                signed[token] += weight * size * (1.0 if side == "BUY" else -1.0)
                gross[token] += weight * size
                prints[token] += 1
    except OSError:
        return out
    for token in token_ids:
        g = gross[token]
        out[token] = {
            "signed_imbalance": signed[token] / g if g > 1e-12 else 0.0,
            "weighted_gross": g,
            "prints": float(prints[token]),
        }
    return out


def canonical_live_flow_features(
    live_flow_path: Path,
    token_ids: set[str],
    *,
    model_sha: str,
    now_ms: int,
    lookback_seconds: int,
    half_life_seconds: float,
    max_publish_age_ms: int,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Consume causally timestamped prints from the full-universe WS owner."""
    out = {
        token: {"signed_imbalance": 0.0, "weighted_gross": 0.0, "prints": 0.0}
        for token in token_ids
    }
    diagnostics: dict[str, Any] = {
        "source": "FULL_UNIVERSE_CPP_WEBSOCKET",
        "valid": False,
        "reason": "UNREAD",
        "publisher_age_ms": None,
        "latest_trade_age_ms": None,
        "matched_prints": 0,
    }
    try:
        payload = json.loads(live_flow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics["reason"] = f"READ_ERROR:{type(exc).__name__}"
        return out, diagnostics
    if (
        payload.get("schema") != LIVE_FLOW_SCHEMA
        or payload.get("producer") != "FAST_STRUCTURAL_CPP_WEBSOCKET"
        or payload.get("model_sha") != model_sha
        or payload.get("paper_only") is not True
        or payload.get("authenticated_execution") is not False
        or payload.get("real_order_submission") is not False
    ):
        diagnostics["reason"] = "CONTRACT_INVALID"
        return out, diagnostics
    published_ms = int(_finite(payload.get("timestamp_ms"), 0.0))
    publish_age_ms = now_ms - published_ms
    diagnostics["publisher_age_ms"] = publish_age_ms
    if publish_age_ms < -5_000 or publish_age_ms > max(1, int(max_publish_age_ms)):
        diagnostics["reason"] = "PUBLISH_STALE"
        return out, diagnostics
    rows = payload.get("rows")
    if not isinstance(rows, list):
        diagnostics["reason"] = "ROWS_INVALID"
        return out, diagnostics
    signed = {token: 0.0 for token in token_ids}
    gross = {token: 0.0 for token in token_ids}
    prints = {token: 0 for token in token_ids}
    latest_receive_ms = 0
    cutoff_ms = now_ms - max(1, int(lookback_seconds)) * 1000
    decay_scale = math.log(2.0) / max(1e-6, float(half_life_seconds))
    seen: set[tuple[str, int, int, str, float, float]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = str(row.get("token_id") or "")
        if token not in token_ids or not isinstance(row.get("trade_prints"), list):
            continue
        for trade in row["trade_prints"]:
            if not isinstance(trade, dict):
                continue
            exchange_ms = int(_finite(trade.get("exchange_ts_ms"), 0.0))
            receive_ms = int(_finite(trade.get("receive_ts_ms"), 0.0))
            side = str(trade.get("side") or "").upper()
            price = _finite(trade.get("price"), -1.0)
            size = _finite(trade.get("size"), -1.0)
            identity = (token, exchange_ms, receive_ms, side, price, size)
            if identity in seen:
                continue
            seen.add(identity)
            if (
                exchange_ms <= 0 or receive_ms < cutoff_ms or receive_ms > now_ms
                or side not in {"BUY", "SELL"} or not 0.0 < price < 1.0 or size <= 0.0
            ):
                continue
            age_seconds = (now_ms - receive_ms) / 1000.0
            weight = math.exp(-decay_scale * age_seconds)
            signed[token] += weight * size * (1.0 if side == "BUY" else -1.0)
            gross[token] += weight * size
            prints[token] += 1
            latest_receive_ms = max(latest_receive_ms, receive_ms)
    for token in token_ids:
        out[token] = {
            "signed_imbalance": signed[token] / gross[token] if gross[token] > 1e-12 else 0.0,
            "weighted_gross": gross[token],
            "prints": float(prints[token]),
        }
    diagnostics.update({
        "valid": True,
        "reason": "OK",
        "latest_trade_age_ms": now_ms - latest_receive_ms if latest_receive_ms else None,
        "matched_prints": sum(prints.values()),
        "producer_raw_last_trade_events": int(_finite(payload.get("raw_last_trade_events"), 0.0)),
        "producer_valid_trade_prints": int(_finite(payload.get("valid_trade_prints"), 0.0)),
    })
    return out, diagnostics


def augment_features(
    feature: tuple[list[float], float, float],
    yes_flow: dict[str, float],
    no_flow: dict[str, float],
) -> tuple[list[float], float, float]:
    x, mid, spread = feature
    return list(x) + [
        max(-1.0, min(1.0, float(yes_flow.get("signed_imbalance", 0.0)))),
        max(-1.0, min(1.0, float(no_flow.get("signed_imbalance", 0.0)))),
    ], mid, spread


def full_depth_vwap(levels: list[tuple[float, float]], shares: float, *, buy: bool) -> float | None:
    target = max(0.0, float(shares))
    if target <= 1e-12:
        return None
    remaining = target
    notional = 0.0
    ordered = sorted(levels, key=lambda item: item[0], reverse=not buy)
    for price, size in ordered:
        px = _finite(price)
        qty = max(0.0, _finite(size, 0.0))
        if not math.isfinite(px) or not 0.0 < px < 1.0 or qty <= 0.0:
            continue
        take = min(remaining, qty)
        notional += take * px
        remaining -= take
        if remaining <= 1e-12:
            return notional / target
    return None


def fee_spec(details: Any) -> economics.FeeSpec:
    return economics.FeeSpec(
        enabled=bool(details.enabled),
        rate=float(details.rate),
        exponent=float(details.exponent),
        taker_only=bool(details.taker_only),
        authoritative=True,
    )


def book_snapshot(
    yes: base.Book,
    no: base.Book,
    liquidity: float,
    *,
    now: int,
    max_age_seconds: int,
) -> economics.BookSnapshot | None:
    source_times = (yes.exchange_ts, no.exchange_ts, yes.received_ts, no.received_ts)
    if any(ts <= 0 or ts > int(now) for ts in source_times):
        return None
    atomic_ws = (
        yes.lineage_continuous and no.lineage_continuous
        and bool(yes.snapshot_id) and yes.snapshot_id == no.snapshot_id
    )
    freshness_ts = min(yes.received_ts, no.received_ts) if atomic_ws else min(source_times)
    snapshot = economics.BookSnapshot(
        yes_bid=yes.bid(), yes_ask=yes.ask(), no_bid=no.bid(), no_ask=no.ask(),
        liquidity=float(liquidity), received_ts=int(freshness_ts),
    )
    if not economics.valid_book(snapshot):
        return None
    age = int(now) - snapshot.received_ts
    if age < 0 or age > max(0, int(max_age_seconds)):
        return None
    return snapshot


def depth_adjusted_economics(
    candidate: economics.RoundTripEconomics,
    *,
    book: base.Book,
    predicted_yes_mid: float,
    fee: economics.FeeSpec,
    shares: float,
    slippage_bps_per_leg: float,
    adverse_markout_penalty_bps: float,
    capital_cost_bps_per_hour: float,
) -> economics.RoundTripEconomics | None:
    if shares <= 0.0 or not fee.authoritative:
        return None
    entry_vwap = full_depth_vwap(list(book.asks), shares, buy=True)
    exit_vwap_now = full_depth_vwap(list(book.bids), shares, buy=False)
    current_side_mid = book.mid()
    if entry_vwap is None or exit_vwap_now is None or not math.isfinite(current_side_mid):
        return None
    predicted_side_mid = predicted_yes_mid if candidate.side == "YES" else 1.0 - predicted_yes_mid
    shift = predicted_side_mid - current_side_mid
    slip = max(0.0, float(slippage_bps_per_leg)) / 10000.0
    entry_price = economics.clamp_probability(entry_vwap * (1.0 + slip))
    expected_exit_price = economics.clamp_probability((exit_vwap_now + shift) * (1.0 - slip))
    entry_fee = economics.fee_per_share(entry_price, fee, taker=True)
    exit_fee = economics.fee_per_share(expected_exit_price, fee, taker=True)
    capital_per_share = entry_price + entry_fee
    adverse_penalty = max(0.0, float(adverse_markout_penalty_bps)) / 10000.0 * capital_per_share
    capital_time_cost = (
        max(0.0, float(capital_cost_bps_per_hour)) / 10000.0
        * (float(candidate.horizon_seconds) / 3600.0) * capital_per_share
    )
    gross_markout = expected_exit_price - entry_price
    net_pnl = gross_markout - entry_fee - exit_fee - candidate.uncertainty_penalty_per_share - adverse_penalty - capital_time_cost
    net_edge = net_pnl / max(capital_per_share, 1e-12)
    return economics.RoundTripEconomics(
        side=candidate.side,
        horizon_seconds=candidate.horizon_seconds,
        entry_price=entry_price,
        expected_exit_price=expected_exit_price,
        entry_fee_per_share=entry_fee,
        exit_fee_per_share=exit_fee,
        gross_markout_per_share=gross_markout,
        uncertainty_penalty_per_share=candidate.uncertainty_penalty_per_share,
        adverse_markout_penalty_per_share=adverse_penalty,
        capital_time_cost_per_share=capital_time_cost,
        net_pnl_per_share=net_pnl,
        capital_per_share=capital_per_share,
        net_edge=net_edge,
        economic_score=net_edge / max(candidate.uncertainty_penalty_per_share, 1e-4),
    )


def conservative_marked_equity(
    cash: float,
    positions: dict[str, Any],
    current: dict[str, tuple[base.Market, base.Book, base.Book, tuple[list[float], float, float]]],
) -> tuple[float, list[dict[str, str]]]:
    value = float(cash)
    unmarkable: list[dict[str, str]] = []
    for market_id, position in positions.items():
        current_row = current.get(market_id)
        if not current_row:
            unmarkable.append({"market_id": market_id, "reason": "missing_current_snapshot"})
            continue
        market, yes, no, _feature = current_row
        if market.fee is None:
            unmarkable.append({"market_id": market_id, "reason": "missing_authoritative_fee"})
            continue
        book = yes if position["side"] == "YES" else no
        shares = float(position["shares"])
        bid_vwap = full_depth_vwap(list(book.bids), shares, buy=False)
        if bid_vwap is None:
            unmarkable.append({"market_id": market_id, "reason": "insufficient_exit_depth"})
            continue
        exit_fee = base.fee_per_share(bid_vwap, market.fee) * shares
        value += max(0.0, shares * bid_vwap - exit_fee)
    return value, unmarkable


def append_fill(run_dir: Path, **row: Any) -> None:
    base.append_csv(
        run_dir / "fills.csv",
        ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "pnl", "net_edge", "expected_exit_price"],
        row,
    )


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:32]


def emit(run_root: Path, event: LedgerEvent) -> None:
    spool_event(run_root, event)


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 fixed-horizon Micro Taker with causal flow and depth-aware round-trip admission")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path)
    parser.add_argument("--live-flow", type=Path)
    parser.add_argument("--flow-lookback-seconds", type=int, default=60)
    parser.add_argument("--flow-half-life-seconds", type=float, default=15.0)
    parser.add_argument("--markets", type=int, default=250)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--horizon-seconds", type=int, default=30)
    parser.add_argument("--max-target-staleness-seconds", type=int, default=10)
    parser.add_argument("--max-trade-usd", type=float, default=125.0)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--uncertainty-z", type=float, default=1.0)
    parser.add_argument("--adverse-markout-bps", type=float, default=2.0)
    parser.add_argument("--capital-cost-bps-per-hour", type=float, default=0.25)
    parser.add_argument("--max-book-age-seconds", type=int, default=5)
    parser.add_argument("--max-positions", type=int, default=20)
    parser.add_argument("--shared-state", type=Path)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--max-shared-publish-age-ms", type=int, default=2500)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("exact 40-hex model SHA required")
    if (
        cfg.get("paper_only") is not True
        or cfg.get("authenticated_execution", False) is not False
        or cfg.get("real_order_submission", False) is not False
    ):
        raise SystemExit("PAPER-only authenticated-disabled config required")
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    start_capital = float(cfg["starting_capital"])
    max_drawdown = float(cfg.get("max_drawdown", 0.15))
    v7 = cfg.get("v7") if isinstance(cfg.get("v7"), dict) else {}
    # The online ridge is not promotion-mature. The sleeve allocation is a
    # ceiling, not permission to concentrate it in one exploratory market.
    max_market_fraction = min(
        float(cfg.get("max_market_fraction", 0.05)),
        max(0.0, float(v7.get("micro_taker_immature_max_market_fraction", 0.005))),
    )
    args.run_dir.mkdir(parents=True, exist_ok=True)
    run_root = args.run_dir.parent
    drain_requested = (args.run_dir.parent / "control" / "CUTOVER_DRAIN").exists()
    trade_tape = args.trade_tape or (args.run_dir.parent / "trade_tape.csv")
    state_path = args.run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"cash": start_capital, "peak": start_capital, "killed": False, "positions": {}, "samples": []}
    prior_dataset_version = int(_finite(state.get("dataset_version"), 1.0))
    legacy_samples = state.get("samples") if isinstance(state.get("samples"), list) else []
    dataset_migration: dict[str, Any] = state.get("dataset_migration") if isinstance(state.get("dataset_migration"), dict) else {}
    if prior_dataset_version < DATASET_VERSION and legacy_samples:
        archive_path = args.run_dir / "state_dataset_v1_degenerate_repeated_snapshot.json"
        if not archive_path.exists():
            archived = dict(state)
            archived["dataset_archive_reason"] = "DEGENERATE_REPEATED_SNAPSHOT"
            archived["superseded_by_dataset_version"] = DATASET_VERSION
            base.atomic_json(archive_path, archived)
        dataset_migration = {
            "from_version": prior_dataset_version,
            "to_version": DATASET_VERSION,
            "reason": "DEGENERATE_REPEATED_SNAPSHOT",
            "archived_samples": len(legacy_samples),
            "archive_path": str(archive_path),
            "migrated_at": int(time.time()),
        }
        state["samples"] = []
    cash = base.finite(state.get("cash"), start_capital)
    peak = max(start_capital, base.finite(state.get("peak"), start_capital))
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    samples = state.get("samples") if isinstance(state.get("samples"), list) else []
    realized_total = base.finite(state.get("realized_pnl_total"), 0.0)
    failures: list[str] = []
    discovered_market_count = 0
    fee_ready_market_count = 0

    try:
        markets = base.discover(gamma, args.markets, args.min_liquidity)
        discovered_market_count = len(markets)
        fee_ready = []
        for market in markets:
            try:
                market.fee = market.fee or base.resolve_fee_details(market.raw, clob, base.request_json)
                fee_ready.append(market)
            except Exception as exc:
                if len(failures) < 30:
                    failures.append(f"fee:{market.id}:{type(exc).__name__}")
        markets = fee_ready
        fee_ready_market_count = len(markets)
        books = (
            base.fetch_shared_books(
                args.shared_state, markets, model_sha=args.model_sha,
                max_publish_age_ms=args.max_shared_publish_age_ms,
            )
            if args.shared_state is not None else base.fetch_books(clob, markets)
        )
    except SharedStateError as exc:
        markets, books = [], {}
        failures.append(f"shared_market_state:{exc}")
    except Exception as exc:
        markets, books = [], {}
        failures.append(f"market_data:{type(exc).__name__}:{exc}")

    now = int(time.time())
    book_pair_count = sum(
        market.yes in books and market.no in books for market in markets
    )
    missing_book_pair_count = max(0, len(markets) - book_pair_count)
    token_ids = {token for market in markets for token in (market.yes, market.no) if token}
    if args.live_flow is not None:
        flow, flow_diagnostics = canonical_live_flow_features(
            args.live_flow, token_ids, model_sha=args.model_sha,
            now_ms=time.time_ns() // 1_000_000,
            lookback_seconds=args.flow_lookback_seconds,
            half_life_seconds=args.flow_half_life_seconds,
            max_publish_age_ms=args.max_shared_publish_age_ms,
        )
    else:
        flow = causal_flow_features(
            trade_tape, token_ids, now=now,
            lookback_seconds=args.flow_lookback_seconds,
            half_life_seconds=args.flow_half_life_seconds,
        )
        flow_diagnostics = {
            "source": "REST_TRADE_TAPE_COMPATIBILITY",
            "valid": False,
            "reason": "NON_CANONICAL_COMPATIBILITY_ONLY",
            "matched_prints": int(sum(row["prints"] for row in flow.values())),
        }
    flow_valid = flow_diagnostics.get("valid") is True
    current: dict[str, tuple[base.Market, base.Book, base.Book, tuple[list[float], float, float]]] = {}
    for market in markets:
        yes, no = books.get(market.yes), books.get(market.no)
        if yes and no:
            snapshot = book_snapshot(yes, no, market.liq, now=now, max_age_seconds=args.max_book_age_seconds)
            if snapshot is None:
                continue
            feature = base.features(yes, no)
            if feature:
                current[market.id] = (market, yes, no, augment_features(feature, flow.get(market.yes, {}), flow.get(market.no, {})))

    label_stats = base.label_matured_samples(samples, now=now, horizon_seconds=args.horizon_seconds, max_target_staleness_seconds=args.max_target_staleness_seconds)
    beta = solve_ridge(samples, 1e-2, FLOW_FEATURE_DIM)
    model_labeled = sum(row.get("y") is not None and isinstance(row.get("x"), list) and len(row["x"]) == FLOW_FEATURE_DIM for row in samples)
    training_diagnostics = sample_diagnostics(
        samples, horizon_seconds=args.horizon_seconds)
    model_valid, model_invalid_reasons = model_validity(training_diagnostics)
    validation_diagnostics = chronological_oos_diagnostics(
        samples, horizon_seconds=args.horizon_seconds)
    if validation_diagnostics.get("valid") is not True:
        model_invalid_reasons.extend(
            str(reason) for reason in validation_diagnostics.get("reasons", []))
        model_valid = False
    # Entry uncertainty must come from untouched future observations, never
    # from residuals of the same rows used to fit beta.
    sigma = validation_diagnostics.get("oos_residual_sigma") if model_valid else None
    slip = max(0.0, args.slippage_bps) / 10000.0

    realized_last_tick = 0.0
    for market_id, position in list(positions.items()):
        current_row = current.get(market_id)
        if not current_row:
            continue
        market, yes, no, feature = current_row
        side = str(position["side"])
        book = yes if side == "YES" else no
        shares = float(position["shares"])
        bid_vwap = full_depth_vwap(list(book.bids), shares, buy=False)
        if bid_vwap is None or market.fee is None:
            if len(failures) < 30:
                failures.append(f"exit_depth:{market_id}")
            continue
        prediction = sum(a * b for a, b in zip(beta, feature[0]))
        prediction = max(-2 * feature[2], min(2 * feature[2], prediction))
        predicted_yes_mid = max(0.001, min(0.999, feature[1] + prediction))
        flip = (side == "YES" and predicted_yes_mid <= feature[1]) or (side == "NO" and predicted_yes_mid >= feature[1])
        if now - int(position["entry_ts"]) >= args.horizon_seconds or flip or bool(state.get("killed")):
            exit_price = max(1e-6, bid_vwap * (1.0 - slip))
            fee = base.fee_per_share(exit_price, market.fee) * shares
            proceeds = exit_price * shares - fee
            pnl = proceeds - float(position["cost"])
            cash += proceeds
            realized_last_tick += pnl
            append_fill(args.run_dir, timestamp=now, market_id=market_id, slug=market.slug, action="SELL_TAKER", side=side, shares=shares, price=exit_price, fee=fee, pnl=pnl, net_edge=position.get("entry_net_edge", ""), expected_exit_price=position.get("expected_exit_price", ""))
            decision_ms = time.time_ns() // 1_000_000
            token = market.yes if side == "YES" else market.no
            position_id = str(position.get("position_id") or stable_id(
                args.model_sha, "MICRO_TAKER", market_id, position.get("entry_ts")))
            exit_order_id = stable_id(position_id, "EXIT", decision_ms)
            exit_fill_id = stable_id(exit_order_id, "FILL")
            exit_common = dict(
                strategy="MICRO_TAKER", model_sha=args.model_sha,
                position_id=position_id, order_id=exit_order_id,
                market_id=market.id, event_id=market.event, token_id=token,
                exchange_ts_ms=book.exchange_ts_ms,
                receive_ts_ms=book.received_ts_ms, decision_ts_ms=decision_ms,
                book_snapshot_id=book.snapshot_id, side="SELL", bid=book.bid(),
                ask=book.ask(), bid_depth=sum(size for _, size in book.bids),
                ask_depth=sum(size for _, size in book.asks), limit_price=exit_price,
                intended_action="TAKER_EXIT", intended_size=shares,
                metadata={"outcome": side, "execution_side": "SELL",
                          "economic_cycle": "MICRO_TAKER_ROUND_TRIP"},
            )
            emit(run_root, LedgerEvent(
                event_type="ORDER_SUBMITTED", order_state="CROSS", **exit_common))
            emit(run_root, LedgerEvent(
                event_type="FILL", fill_id=exit_fill_id, exchange_ts_ms=book.exchange_ts_ms,
                receive_ts_ms=book.received_ts_ms, strategy="MICRO_TAKER",
                model_sha=args.model_sha, position_id=position_id, order_id=exit_order_id,
                market_id=market.id, event_id=market.event, token_id=token, side="SELL",
                fill_price=exit_price, filled_size=shares, complete=True, fee=fee,
                fee_rate=market.fee.rate, fee_source=market.fee.source,
                slippage=shares * abs(exit_price - bid_vwap),
                metadata={"outcome": side, "execution_side": "SELL"}))
            emit(run_root, LedgerEvent(
                event_type="FINAL", strategy="MICRO_TAKER", model_sha=args.model_sha,
                position_id=position_id, market_id=market.id, event_id=market.event,
                token_id=token, final_pnl=pnl, realized_cashflow=pnl,
                fee=0.0, slippage=0.0, unwind_loss=0.0,
                capital_cost=0.0, latency_cost=0.0,
                capital_duration_ms=max(0, (now - int(position["entry_ts"])) * 1000),
                metadata={"terminal_state": "ROUND_TRIP_CLOSED", "outcome": side,
                          "realized": True, "unwind_accounted": True,
                          "cost_vector_complete": True,
                          "terminal_id": f"micro_taker:{position_id}:final",
                          "pnl_decomposition": {"trading_pnl": pnl,
                              "spread_capture": 0.0, "adverse_markout": 0.0,
                              "inventory_pnl": 0.0, "maker_rebates": 0.0,
                              "liquidity_rewards": 0.0,
                              "own_reward_share_verified": False},
                          "entry_cost": float(position["cost"]), "exit_proceeds": proceeds,
                          "entry_fill_id": position.get("entry_fill_id"),
                          "exit_fill_id": exit_fill_id}))
            del positions[market_id]
    realized_total += realized_last_tick

    equity, unmarkable_positions = conservative_marked_equity(cash, positions, current)
    new_risk_frozen = bool(unmarkable_positions) or drain_requested
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    marking_complete = not unmarkable_positions
    killed = bool(state.get("killed")) or (marking_complete and drawdown >= max_drawdown)

    signals = 0
    opened = 0
    best_edge = 0.0
    admission_rows: list[dict[str, Any]] = []
    rejection_funnel: dict[str, int] = {
        "feature_ready_markets": len(current),
        "model_or_flow_gate_closed": 0,
        "already_positioned": 0,
        "missing_fee": 0,
        "prediction_evaluated": 0,
        "no_complete_round_trip_ev": 0,
        "zero_capital_room": 0,
        "entry_or_exit_depth_rejected": 0,
        "nonpositive_net_edge": 0,
        "ranked_signals": 0,
        "below_minimum_order": 0,
        "cash_rejected": 0,
        "opened": 0,
    }
    if not killed and not new_risk_frozen and model_valid and flow_valid:
        ranked: list[tuple[float, Any, str, base.Book, economics.RoundTripEconomics, float]] = []
        for market_id, (market, yes, no, feature) in current.items():
            if market_id in positions or market.fee is None:
                if market_id in positions:
                    rejection_funnel["already_positioned"] += 1
                else:
                    rejection_funnel["missing_fee"] += 1
                continue
            rejection_funnel["prediction_evaluated"] += 1
            prediction = sum(a * b for a, b in zip(beta, feature[0]))
            prediction = max(-2 * feature[2], min(2 * feature[2], prediction))
            predicted_yes_mid = max(0.001, min(0.999, feature[1] + prediction))
            snapshot = book_snapshot(yes, no, market.liq, now=now, max_age_seconds=args.max_book_age_seconds)
            if snapshot is None:
                continue
            candidate = economics.choose_side(
                book=snapshot,
                predicted_yes_mid=predicted_yes_mid,
                prediction_sigma_probability=float(sigma),
                fee=fee_spec(market.fee),
                horizon_seconds=args.horizon_seconds,
                now=now,
                slippage_bps_per_leg=args.slippage_bps,
                uncertainty_z=args.uncertainty_z,
                adverse_markout_penalty_bps=args.adverse_markout_bps,
                capital_cost_bps_per_hour=args.capital_cost_bps_per_hour,
                max_book_age_seconds=args.max_book_age_seconds,
                minimum_net_edge=args.min_edge,
            )
            if candidate is None:
                rejection_funnel["no_complete_round_trip_ev"] += 1
                continue
            book = yes if candidate.side == "YES" else no
            room_probe = max(0.0, min(args.max_trade_usd, max_market_fraction * equity, cash))
            if room_probe <= 0.0:
                rejection_funnel["zero_capital_room"] += 1
                continue
            shares_probe = room_probe / max(candidate.capital_per_share, 1e-9)
            adjusted = depth_adjusted_economics(candidate, book=book, predicted_yes_mid=predicted_yes_mid, fee=fee_spec(market.fee), shares=shares_probe, slippage_bps_per_leg=args.slippage_bps, adverse_markout_penalty_bps=args.adverse_markout_bps, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
            if adjusted is None:
                rejection_funnel["entry_or_exit_depth_rejected"] += 1
                continue
            shares_probe = room_probe / max(adjusted.capital_per_share, 1e-9)
            adjusted = depth_adjusted_economics(adjusted, book=book, predicted_yes_mid=predicted_yes_mid, fee=fee_spec(market.fee), shares=shares_probe, slippage_bps_per_leg=args.slippage_bps, adverse_markout_penalty_bps=args.adverse_markout_bps, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
            if adjusted is None or adjusted.net_edge < args.min_edge or adjusted.net_pnl_per_share <= 0.0:
                if adjusted is None:
                    rejection_funnel["entry_or_exit_depth_rejected"] += 1
                else:
                    rejection_funnel["nonpositive_net_edge"] += 1
                continue
            ranked.append((adjusted.economic_score, market, adjusted.side, book, adjusted, predicted_yes_mid))
            admission_rows.append({
                "market_id": market.id,
                "side": adjusted.side,
                "net_edge": adjusted.net_edge,
                "net_pnl_per_share": adjusted.net_pnl_per_share,
                "entry_price": adjusted.entry_price,
                "expected_exit_price": adjusted.expected_exit_price,
                "entry_fee_per_share": adjusted.entry_fee_per_share,
                "exit_fee_per_share": adjusted.exit_fee_per_share,
                "uncertainty_penalty_per_share": adjusted.uncertainty_penalty_per_share,
                "adverse_markout_penalty_per_share": adjusted.adverse_markout_penalty_per_share,
                "capital_time_cost_per_share": adjusted.capital_time_cost_per_share,
                "yes_flow_imbalance": feature[0][-2],
                "no_flow_imbalance": feature[0][-1],
                "depth_contract": "full_visible_depth_entry_and_forecast_shifted_exit_vwap",
            })
        ranked.sort(reverse=True, key=lambda row: row[0])
        signals = len(ranked)
        rejection_funnel["ranked_signals"] = signals
        best_edge = max((row[4].net_edge for row in ranked), default=0.0)
        for _score, market, side, book, candidate, predicted_yes_mid in ranked:
            if len(positions) >= args.max_positions:
                break
            if market.id in positions or market.fee is None:
                continue
            room = max(0.0, min(args.max_trade_usd, max_market_fraction * equity, cash))
            shares = room / max(candidate.capital_per_share, 1e-9)
            candidate = depth_adjusted_economics(candidate, book=book, predicted_yes_mid=predicted_yes_mid, fee=fee_spec(market.fee), shares=shares, slippage_bps_per_leg=args.slippage_bps, adverse_markout_penalty_bps=args.adverse_markout_bps, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
            if candidate is None or candidate.net_edge < args.min_edge or candidate.net_pnl_per_share <= 0.0:
                continue
            shares = room / max(candidate.capital_per_share, 1e-9)
            if shares < book.min_order:
                rejection_funnel["below_minimum_order"] += 1
                continue
            candidate = depth_adjusted_economics(candidate, book=book, predicted_yes_mid=predicted_yes_mid, fee=fee_spec(market.fee), shares=shares, slippage_bps_per_leg=args.slippage_bps, adverse_markout_penalty_bps=args.adverse_markout_bps, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
            if candidate is None or candidate.net_edge < args.min_edge or candidate.net_pnl_per_share <= 0.0:
                if candidate is None:
                    rejection_funnel["entry_or_exit_depth_rejected"] += 1
                else:
                    rejection_funnel["nonpositive_net_edge"] += 1
                continue
            fee = candidate.entry_fee_per_share * shares
            cost = candidate.entry_price * shares + fee
            if cost > cash + 1e-9:
                rejection_funnel["cash_rejected"] += 1
                continue
            positions[market.id] = {
                "side": side,
                "shares": shares,
                "entry_price": candidate.entry_price,
                "cost": cost,
                "entry_ts": now,
                "entry_net_edge": candidate.net_edge,
                "expected_exit_price": candidate.expected_exit_price,
            }
            cash -= cost
            opened += 1
            rejection_funnel["opened"] += 1
            append_fill(args.run_dir, timestamp=now, market_id=market.id, slug=market.slug, action="BUY_TAKER", side=side, shares=shares, price=candidate.entry_price, fee=fee, pnl=0.0, net_edge=candidate.net_edge, expected_exit_price=candidate.expected_exit_price)
            decision_ms = time.time_ns() // 1_000_000
            position_id = stable_id(args.model_sha, "MICRO_TAKER", market.id, decision_ms)
            candidate_id = stable_id(position_id, "CANDIDATE")
            order_id = stable_id(position_id, "ENTRY_ORDER")
            fill_id = stable_id(order_id, "FILL")
            common = dict(
                strategy="MICRO_TAKER", model_sha=args.model_sha,
                position_id=position_id, market_id=market.id, event_id=market.event,
                token_id=book.token, exchange_ts_ms=book.exchange_ts_ms,
                receive_ts_ms=book.received_ts_ms, decision_ts_ms=decision_ms,
                book_snapshot_id=book.snapshot_id, side="BUY", bid=book.bid(), ask=book.ask(),
                bid_depth=sum(size for _, size in book.bids),
                ask_depth=sum(size for _, size in book.asks),
                limit_price=candidate.entry_price, predicted_alpha=candidate.gross_markout_per_share,
                expected_ev=candidate.net_pnl_per_share, intended_action="TAKER_ENTRY",
                intended_size=shares, metadata={"outcome": side, "execution_side": "BUY",
                    "horizon_seconds": candidate.horizon_seconds,
                    "uncertainty_penalty_per_share": candidate.uncertainty_penalty_per_share,
                    "market_state_source": "SHARED_CPP_WEBSOCKET" if args.shared_state else "REST_COMPATIBILITY"},
            )
            emit(run_root, LedgerEvent(event_type="CANDIDATE", candidate_id=candidate_id, **common))
            emit(run_root, LedgerEvent(
                event_type="ORDER_SUBMITTED", candidate_id=candidate_id,
                order_id=order_id, order_state="CROSS", **common))
            emit(run_root, LedgerEvent(
                event_type="FILL", strategy="MICRO_TAKER", model_sha=args.model_sha,
                position_id=position_id, order_id=order_id, fill_id=fill_id,
                market_id=market.id, event_id=market.event, token_id=book.token,
                exchange_ts_ms=book.exchange_ts_ms, receive_ts_ms=book.received_ts_ms,
                side="BUY", fill_price=candidate.entry_price, filled_size=shares,
                complete=True, fee=fee, fee_rate=market.fee.rate,
                fee_source=market.fee.source,
                slippage=shares * abs(candidate.entry_price - (
                    full_depth_vwap(list(book.asks), shares, buy=True) or candidate.entry_price)),
                metadata={"outcome": side, "execution_side": "BUY"}))
            positions[market.id].update({
                "position_id": position_id, "entry_order_id": order_id,
                "entry_fill_id": fill_id, "markout_horizons": [],
            })
    else:
        rejection_funnel["model_or_flow_gate_closed"] = len(current)

    # Append one size-aware executable mark per configured economic horizon.
    for market_id, position in positions.items():
        current_row = current.get(market_id)
        if not current_row or not position.get("entry_fill_id"):
            continue
        market, yes, no, _feature = current_row
        side, shares = str(position["side"]), float(position["shares"])
        book = yes if side == "YES" else no
        bid_vwap = full_depth_vwap(list(book.bids), shares, buy=False)
        if bid_vwap is None:
            continue
        emitted = set(int(value) for value in position.get("markout_horizons", []))
        age = max(0, now - int(position["entry_ts"]))
        for horizon in (5, 15, 30, 60, 300):
            if horizon in emitted or age < horizon:
                continue
            fee = base.fee_per_share(bid_vwap, market.fee) * shares if market.fee else 0.0
            liquidation = max(0.0, shares * bid_vwap - fee)
            emit(run_root, LedgerEvent(
                event_type="MARKOUT", strategy="MICRO_TAKER", model_sha=args.model_sha,
                position_id=str(position["position_id"]), order_id=str(position["entry_order_id"]),
                fill_id=str(position["entry_fill_id"]), market_id=market.id,
                event_id=market.event, token_id=book.token,
                exchange_ts_ms=book.exchange_ts_ms, receive_ts_ms=book.received_ts_ms,
                book_snapshot_id=book.snapshot_id,
                executable_liquidation_value=liquidation,
                markouts={f"{horizon}s": liquidation - float(position["cost"])},
                metadata={"outcome": side, "full_depth": True, "fee_net": True}))
            emitted.add(horizon)
        position["markout_horizons"] = sorted(emitted)

    known_sample_keys = {
        str(row.get("sample_key") or "") for row in samples
        if str(row.get("sample_key") or "")
    }
    novel_samples = 0
    duplicate_snapshots_rejected = 0
    for market_id, (market, yes, no, feature) in current.items():
        if not flow_valid:
            break
        key = sample_key(market_id, yes, no)
        if key in known_sample_keys:
            duplicate_snapshots_rejected += 1
            continue
        observed_ts = max(yes.source_received_ts, no.source_received_ts)
        samples.append({
            "dataset_version": DATASET_VERSION,
            "dataset_lineage": DATASET_LINEAGE,
            "sample_key": key,
            "ts": observed_ts,
            "snapshot_published_ts": max(yes.snapshot_published_ts, no.snapshot_published_ts),
            "market_id": market_id,
            "event_id": market.event,
            "yes_token": market.yes,
            "no_token": market.no,
            "yes_state_version": yes.state_version,
            "no_state_version": no.state_version,
            "yes_lineage_epoch": yes.lineage_epoch,
            "no_lineage_epoch": no.lineage_epoch,
            "mid": feature[1], "spread": feature[2], "x": feature[0], "y": None,
        })
        known_sample_keys.add(key)
        novel_samples += 1
    samples = samples[-50000:]
    equity, unmarkable_positions = conservative_marked_equity(cash, positions, current)
    new_risk_frozen = bool(unmarkable_positions) or drain_requested
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    marking_complete = not unmarkable_positions
    killed = killed or (marking_complete and drawdown >= max_drawdown)
    labeled = sum(row.get("y") is not None for row in samples)
    model_labeled = sum(row.get("y") is not None and isinstance(row.get("x"), list) and len(row["x"]) == FLOW_FEATURE_DIM for row in samples)
    dataset_diagnostics = sample_diagnostics(
        samples, horizon_seconds=args.horizon_seconds)

    new_state = {
        "schema": "polymarket_v7_micro_taker_status_v1",
        "timestamp": now,
        "model_sha": args.model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "cash": cash,
        "equity": equity,
        "peak": peak,
        "drawdown": drawdown,
        "killed": killed,
        "new_risk_frozen": new_risk_frozen,
        "drain_requested": drain_requested,
        "drain_complete": drain_requested and not positions,
        "marking_complete": marking_complete,
        "market_capital_ceiling": start_capital * max_market_fraction,
        "unmarkable_positions": unmarkable_positions,
        "marking_contract": CONSERVATIVE_MARKING_CONTRACT,
        "positions": positions,
        "samples": samples,
        "dataset_version": DATASET_VERSION,
        "dataset_lineage": DATASET_LINEAGE,
        "dataset_migration": dataset_migration,
        "dataset_diagnostics": dataset_diagnostics,
        "novel_samples_last_tick": novel_samples,
        "duplicate_snapshots_rejected_last_tick": duplicate_snapshots_rejected,
        "beta": beta,
        "prediction_sigma_probability": sigma,
        "model_valid": model_valid,
        "model_invalid_reasons": model_invalid_reasons,
        "validation_diagnostics": validation_diagnostics,
        "flow_valid": flow_valid,
        "flow_diagnostics": flow_diagnostics,
        "rejection_funnel": rejection_funnel,
        "labeled_samples": labeled,
        "model_labeled_samples": model_labeled,
        "market_state_source": (
            "SHARED_CPP_WEBSOCKET" if args.shared_state else "REST_COMPATIBILITY"
        ),
        "discovered_markets": discovered_market_count,
        "fee_ready_markets": fee_ready_market_count,
        "atomic_book_pairs": book_pair_count,
        "missing_book_pairs": missing_book_pair_count,
        "feature_ready_markets": len(current),
        "label_stats_last_tick": label_stats,
        "signals": signals,
        "opened": opened,
        "best_edge": best_edge,
        "realized_pnl_last_tick": realized_last_tick,
        "realized_pnl_total": realized_total,
        "admission_contract": "causal_flow_depth_complete_round_trip_ev",
        "execution_contract": COMPLETE_ROUND_TRIP_EXECUTION_CONTRACT,
        "feature_contract": "book_microprice_depth_parity_plus_receive_causal_event_decayed_yes_no_taker_flow",
        "exit_liquidity_contract": "shares_specific_full_visible_bid_depth_vwap_fail_closed",
        "failures": failures,
    }
    base.atomic_json(state_path, new_state)
    base.atomic_json(args.run_dir / "status.json", {k: new_state[k] for k in (
        "schema", "timestamp", "model_sha", "paper_only", "authenticated_execution", "real_order_submission",
        "cash", "equity", "peak", "drawdown", "killed",
        "new_risk_frozen", "drain_requested", "drain_complete", "marking_complete", "market_capital_ceiling",
        "unmarkable_positions", "marking_contract",
        "prediction_sigma_probability", "model_valid", "model_invalid_reasons",
        "validation_diagnostics",
        "flow_valid", "flow_diagnostics",
        "rejection_funnel",
        "dataset_version", "dataset_lineage", "dataset_diagnostics",
        "novel_samples_last_tick", "duplicate_snapshots_rejected_last_tick",
        "labeled_samples", "model_labeled_samples",
        "market_state_source", "discovered_markets", "fee_ready_markets",
        "atomic_book_pairs", "missing_book_pairs", "feature_ready_markets",
        "signals", "opened", "best_edge",
        "realized_pnl_last_tick", "realized_pnl_total", "admission_contract", "execution_contract", "feature_contract", "exit_liquidity_contract", "failures"
    )} | {"open_positions": len(positions)})
    base.atomic_json(args.run_dir / "admission_latest.json", {
        "timestamp": now,
        "paper_only": True,
        "contract": COMPLETE_ROUND_TRIP_EXECUTION_CONTRACT,
        "details": "causal-flow + full-depth-entry/exit + fees/slippage/uncertainty/adverse/capital-time",
        "new_risk_frozen": new_risk_frozen,
        "unmarkable_positions": unmarkable_positions,
        "rows": admission_rows[:100],
    })
    base.append_csv(
        args.run_dir / "equity.csv",
        ["timestamp", "cash", "equity", "drawdown", "open_positions", "signals", "opened", "best_edge", "labeled_samples", "model_labeled_samples", "realized_pnl_total", "prediction_sigma_probability"],
        {
            "timestamp": now, "cash": cash, "equity": equity, "drawdown": drawdown,
            "open_positions": len(positions), "signals": signals, "opened": opened,
            "best_edge": best_edge, "labeled_samples": labeled, "model_labeled_samples": model_labeled,
            "realized_pnl_total": realized_total, "prediction_sigma_probability": sigma,
        },
    )
    print(json.dumps({
        "markets": len(markets), "atomic_book_pairs": book_pair_count,
        "feature_ready_markets": len(current), "labeled": labeled, "model_labeled": model_labeled,
        "signals": signals, "opened": opened, "positions": len(positions), "equity": equity,
        "realized_pnl_total": realized_total, "best_edge": best_edge,
        "prediction_sigma_probability": sigma, "model_valid": model_valid,
        "model_invalid_reasons": model_invalid_reasons,
        "novel_samples": novel_samples,
        "duplicate_snapshots_rejected": duplicate_snapshots_rejected,
        "killed": killed,
        "new_risk_frozen": new_risk_frozen, "unmarkable_positions": len(unmarkable_positions),
        "marking_contract": CONSERVATIVE_MARKING_CONTRACT,
        "admission_contract": "causal_flow_depth_complete_round_trip_ev",
        "execution_contract": COMPLETE_ROUND_TRIP_EXECUTION_CONTRACT,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
