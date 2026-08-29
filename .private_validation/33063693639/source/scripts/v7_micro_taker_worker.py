#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import v7_micro_taker_data as base
import v7_micro_taker_core as economics

FLOW_FEATURE_DIM = 8
COMPLETE_ROUND_TRIP_EXECUTION_CONTRACT = "complete_round_trip_executable_ev"
CONSERVATIVE_MARKING_CONTRACT = "full_depth_executable_bid_net_fee_or_zero_fail_closed"


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
    freshness_ts = min(source_times)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 fixed-horizon Micro Taker with causal flow and depth-aware round-trip admission")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path)
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
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    start_capital = float(cfg["starting_capital"])
    max_drawdown = float(cfg.get("max_drawdown", 0.15))
    max_market_fraction = float(cfg.get("max_market_fraction", 0.05))
    args.run_dir.mkdir(parents=True, exist_ok=True)
    trade_tape = args.trade_tape or (args.run_dir.parent / "trade_tape.csv")
    state_path = args.run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"cash": start_capital, "peak": start_capital, "killed": False, "positions": {}, "samples": []}
    cash = base.finite(state.get("cash"), start_capital)
    peak = max(start_capital, base.finite(state.get("peak"), start_capital))
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    samples = state.get("samples") if isinstance(state.get("samples"), list) else []
    realized_total = base.finite(state.get("realized_pnl_total"), 0.0)
    failures: list[str] = []

    try:
        markets = base.discover(gamma, args.markets, args.min_liquidity)
        fee_ready = []
        for market in markets:
            try:
                market.fee = market.fee or base.resolve_fee_details(market.raw, clob, base.request_json)
                fee_ready.append(market)
            except Exception as exc:
                if len(failures) < 30:
                    failures.append(f"fee:{market.id}:{type(exc).__name__}")
        markets = fee_ready
        books = base.fetch_books(clob, markets)
    except Exception as exc:
        markets, books = [], {}
        failures.append(f"market_data:{type(exc).__name__}:{exc}")

    now = int(time.time())
    token_ids = {token for market in markets for token in (market.yes, market.no) if token}
    flow = causal_flow_features(trade_tape, token_ids, now=now, lookback_seconds=args.flow_lookback_seconds, half_life_seconds=args.flow_half_life_seconds)
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
    sigma = residual_sigma(samples, beta)
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
            del positions[market_id]
    realized_total += realized_last_tick

    equity, unmarkable_positions = conservative_marked_equity(cash, positions, current)
    new_risk_frozen = bool(unmarkable_positions)
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    killed = bool(state.get("killed")) or drawdown >= max_drawdown

    signals = 0
    opened = 0
    best_edge = 0.0
    admission_rows: list[dict[str, Any]] = []
    if not killed and not new_risk_frozen and model_labeled >= 40:
        ranked: list[tuple[float, Any, str, base.Book, economics.RoundTripEconomics, float]] = []
        for market_id, (market, yes, no, feature) in current.items():
            if market_id in positions or market.fee is None:
                continue
            prediction = sum(a * b for a, b in zip(beta, feature[0]))
            prediction = max(-2 * feature[2], min(2 * feature[2], prediction))
            predicted_yes_mid = max(0.001, min(0.999, feature[1] + prediction))
            snapshot = book_snapshot(yes, no, market.liq, now=now, max_age_seconds=args.max_book_age_seconds)
            if snapshot is None:
                continue
            candidate = economics.choose_side(
                book=snapshot,
                predicted_yes_mid=predicted_yes_mid,
                prediction_sigma_probability=sigma,
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
                continue
            book = yes if candidate.side == "YES" else no
            room_probe = max(0.0, min(args.max_trade_usd, max_market_fraction * equity, cash))
            shares_probe = room_probe / max(candidate.capital_per_share, 1e-9)
            adjusted = depth_adjusted_economics(candidate, book=book, predicted_yes_mid=predicted_yes_mid, fee=fee_spec(market.fee), shares=shares_probe, slippage_bps_per_leg=args.slippage_bps, adverse_markout_penalty_bps=args.adverse_markout_bps, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
            if adjusted is None:
                continue
            shares_probe = room_probe / max(adjusted.capital_per_share, 1e-9)
            adjusted = depth_adjusted_economics(adjusted, book=book, predicted_yes_mid=predicted_yes_mid, fee=fee_spec(market.fee), shares=shares_probe, slippage_bps_per_leg=args.slippage_bps, adverse_markout_penalty_bps=args.adverse_markout_bps, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
            if adjusted is None or adjusted.net_edge < args.min_edge or adjusted.net_pnl_per_share <= 0.0:
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
                continue
            candidate = depth_adjusted_economics(candidate, book=book, predicted_yes_mid=predicted_yes_mid, fee=fee_spec(market.fee), shares=shares, slippage_bps_per_leg=args.slippage_bps, adverse_markout_penalty_bps=args.adverse_markout_bps, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
            if candidate is None or candidate.net_edge < args.min_edge or candidate.net_pnl_per_share <= 0.0:
                continue
            fee = candidate.entry_fee_per_share * shares
            cost = candidate.entry_price * shares + fee
            if cost > cash + 1e-9:
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
            append_fill(args.run_dir, timestamp=now, market_id=market.id, slug=market.slug, action="BUY_TAKER", side=side, shares=shares, price=candidate.entry_price, fee=fee, pnl=0.0, net_edge=candidate.net_edge, expected_exit_price=candidate.expected_exit_price)

    for market_id, (_market, _yes, _no, feature) in current.items():
        samples.append({"ts": now, "market_id": market_id, "mid": feature[1], "spread": feature[2], "x": feature[0], "y": None})
    samples = samples[-50000:]
    equity, unmarkable_positions = conservative_marked_equity(cash, positions, current)
    new_risk_frozen = bool(unmarkable_positions)
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    killed = killed or drawdown >= max_drawdown
    labeled = sum(row.get("y") is not None for row in samples)
    model_labeled = sum(row.get("y") is not None and isinstance(row.get("x"), list) and len(row["x"]) == FLOW_FEATURE_DIM for row in samples)

    new_state = {
        "timestamp": now,
        "paper_only": True,
        "authenticated_execution": False,
        "cash": cash,
        "equity": equity,
        "peak": peak,
        "drawdown": drawdown,
        "killed": killed,
        "new_risk_frozen": new_risk_frozen,
        "unmarkable_positions": unmarkable_positions,
        "marking_contract": CONSERVATIVE_MARKING_CONTRACT,
        "positions": positions,
        "samples": samples,
        "beta": beta,
        "prediction_sigma_probability": sigma,
        "labeled_samples": labeled,
        "model_labeled_samples": model_labeled,
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
        "timestamp", "paper_only", "authenticated_execution", "cash", "equity", "peak", "drawdown", "killed",
        "new_risk_frozen", "unmarkable_positions", "marking_contract",
        "prediction_sigma_probability", "labeled_samples", "model_labeled_samples", "signals", "opened", "best_edge",
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
        "markets": len(markets), "labeled": labeled, "model_labeled": model_labeled,
        "signals": signals, "opened": opened, "positions": len(positions), "equity": equity,
        "realized_pnl_total": realized_total, "best_edge": best_edge,
        "prediction_sigma_probability": sigma, "killed": killed,
        "new_risk_frozen": new_risk_frozen, "unmarkable_positions": len(unmarkable_positions),
        "marking_contract": CONSERVATIVE_MARKING_CONTRACT,
        "admission_contract": "causal_flow_depth_complete_round_trip_ev",
        "execution_contract": COMPLETE_ROUND_TRIP_EXECUTION_CONTRACT,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
