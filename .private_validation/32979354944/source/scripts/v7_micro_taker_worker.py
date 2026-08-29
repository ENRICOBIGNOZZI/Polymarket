#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import v6_micro_taker as base
import v7_micro_taker_core as economics


def residual_sigma(samples: list[dict[str, Any]], beta: list[float]) -> float:
    residuals: list[float] = []
    for row in samples[-10000:]:
        if row.get("y") is None:
            continue
        try:
            x = [float(value) for value in row["x"]]
            y = float(row["y"])
        except (KeyError, TypeError, ValueError):
            continue
        prediction = sum(a * b for a, b in zip(beta, x))
        residuals.append(y - prediction)
    if len(residuals) < 20:
        return 0.02
    return max(1e-4, statistics.stdev(residuals))


def fee_spec(details: Any) -> economics.FeeSpec:
    return economics.FeeSpec(
        enabled=bool(details.enabled),
        rate=float(details.rate),
        exponent=float(details.exponent),
        taker_only=bool(details.taker_only),
        authoritative=True,
    )


def book_snapshot(yes: base.Book, no: base.Book, liquidity: float, received_ts: int) -> economics.BookSnapshot | None:
    snapshot = economics.BookSnapshot(
        yes_bid=yes.bid(), yes_ask=yes.ask(), no_bid=no.bid(), no_ask=no.ask(),
        liquidity=float(liquidity), received_ts=int(received_ts),
    )
    return snapshot if economics.valid_book(snapshot) else None


def append_fill(run_dir: Path, **row: Any) -> None:
    base.append_csv(
        run_dir / "fills.csv",
        ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "pnl", "net_edge", "expected_exit_price"],
        row,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 fixed-horizon Micro Taker with complete executable round-trip admission")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
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
    now = int(time.time())
    args.run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.run_dir / "state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {"cash": start_capital, "peak": start_capital, "killed": False, "positions": {}, "samples": []}
    )
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

    current: dict[str, tuple[base.Market, base.Book, base.Book, tuple[list[float], float, float]]] = {}
    for market in markets:
        yes, no = books.get(market.yes), books.get(market.no)
        if yes and no:
            feature = base.features(yes, no)
            if feature:
                current[market.id] = (market, yes, no, feature)

    label_stats = base.label_matured_samples(
        samples,
        now=now,
        horizon_seconds=args.horizon_seconds,
        max_target_staleness_seconds=args.max_target_staleness_seconds,
    )
    beta = base.solve_ridge(samples, 1e-2)
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
        bid = book.bid()
        if not math.isfinite(bid) or market.fee is None:
            continue
        prediction = sum(a * b for a, b in zip(beta, feature[0]))
        prediction = max(-2 * feature[2], min(2 * feature[2], prediction))
        predicted_yes_mid = max(0.001, min(0.999, feature[1] + prediction))
        flip = (side == "YES" and predicted_yes_mid <= feature[1]) or (side == "NO" and predicted_yes_mid >= feature[1])
        if now - int(position["entry_ts"]) >= args.horizon_seconds or flip or bool(state.get("killed")):
            exit_price = max(1e-6, bid * (1.0 - slip))
            shares = float(position["shares"])
            fee = base.fee_per_share(exit_price, market.fee) * shares
            proceeds = exit_price * shares - fee
            pnl = proceeds - float(position["cost"])
            cash += proceeds
            realized_last_tick += pnl
            append_fill(
                args.run_dir,
                timestamp=now, market_id=market_id, slug=market.slug, action="SELL_TAKER",
                side=side, shares=shares, price=exit_price, fee=fee, pnl=pnl,
                net_edge=position.get("entry_net_edge", ""), expected_exit_price=position.get("expected_exit_price", ""),
            )
            del positions[market_id]
    realized_total += realized_last_tick

    def marked_equity() -> float:
        value = cash
        for market_id, position in positions.items():
            current_row = current.get(market_id)
            if not current_row:
                value += float(position["shares"]) * float(position["entry_price"])
                continue
            book = current_row[1] if position["side"] == "YES" else current_row[2]
            bid = book.bid()
            value += float(position["shares"]) * (bid if math.isfinite(bid) else float(position["entry_price"]))
        return value

    equity = marked_equity()
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    killed = bool(state.get("killed")) or drawdown >= max_drawdown

    signals = 0
    opened = 0
    best_edge = 0.0
    admission_rows: list[dict[str, Any]] = []
    labeled = sum(row.get("y") is not None for row in samples)
    if not killed and labeled >= 40:
        ranked: list[tuple[float, Any, str, base.Book, economics.RoundTripEconomics]] = []
        for market_id, (market, yes, no, feature) in current.items():
            if market_id in positions or market.fee is None:
                continue
            prediction = sum(a * b for a, b in zip(beta, feature[0]))
            prediction = max(-2 * feature[2], min(2 * feature[2], prediction))
            predicted_yes_mid = max(0.001, min(0.999, feature[1] + prediction))
            snapshot = book_snapshot(yes, no, market.liq, now)
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
            ranked.append((candidate.economic_score, market, candidate.side, book, candidate))
            admission_rows.append({
                "market_id": market.id,
                "side": candidate.side,
                "net_edge": candidate.net_edge,
                "net_pnl_per_share": candidate.net_pnl_per_share,
                "entry_price": candidate.entry_price,
                "expected_exit_price": candidate.expected_exit_price,
                "entry_fee_per_share": candidate.entry_fee_per_share,
                "exit_fee_per_share": candidate.exit_fee_per_share,
                "uncertainty_penalty_per_share": candidate.uncertainty_penalty_per_share,
                "adverse_markout_penalty_per_share": candidate.adverse_markout_penalty_per_share,
                "capital_time_cost_per_share": candidate.capital_time_cost_per_share,
            })
        ranked.sort(reverse=True, key=lambda row: row[0])
        signals = len(ranked)
        best_edge = max((row[4].net_edge for row in ranked), default=0.0)
        for _score, market, side, book, candidate in ranked:
            if len(positions) >= args.max_positions:
                break
            if market.id in positions:
                continue
            room = max(0.0, min(args.max_trade_usd, max_market_fraction * equity, cash))
            per_share_cost = candidate.entry_price + candidate.entry_fee_per_share
            shares = room / max(per_share_cost, 1e-9)
            if shares < book.min_order:
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
            append_fill(
                args.run_dir,
                timestamp=now, market_id=market.id, slug=market.slug, action="BUY_TAKER",
                side=side, shares=shares, price=candidate.entry_price, fee=fee, pnl=0.0,
                net_edge=candidate.net_edge, expected_exit_price=candidate.expected_exit_price,
            )

    for market_id, (_market, _yes, _no, feature) in current.items():
        samples.append({"ts": now, "market_id": market_id, "mid": feature[1], "spread": feature[2], "x": feature[0], "y": None})
    samples = samples[-50000:]
    equity = marked_equity()
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    killed = killed or drawdown >= max_drawdown
    labeled = sum(row.get("y") is not None for row in samples)

    new_state = {
        "timestamp": now,
        "paper_only": True,
        "authenticated_execution": False,
        "cash": cash,
        "equity": equity,
        "peak": peak,
        "drawdown": drawdown,
        "killed": killed,
        "positions": positions,
        "samples": samples,
        "beta": beta,
        "prediction_sigma_probability": sigma,
        "labeled_samples": labeled,
        "label_stats_last_tick": label_stats,
        "signals": signals,
        "opened": opened,
        "best_edge": best_edge,
        "realized_pnl_last_tick": realized_last_tick,
        "realized_pnl_total": realized_total,
        "admission_contract": "complete_round_trip_executable_ev",
        "failures": failures,
    }
    base.atomic_json(state_path, new_state)
    base.atomic_json(args.run_dir / "status.json", {k: new_state[k] for k in (
        "timestamp", "paper_only", "authenticated_execution", "cash", "equity", "peak", "drawdown", "killed",
        "prediction_sigma_probability", "labeled_samples", "signals", "opened", "best_edge",
        "realized_pnl_last_tick", "realized_pnl_total", "admission_contract", "failures"
    )} | {"open_positions": len(positions)})
    base.atomic_json(args.run_dir / "admission_latest.json", {
        "timestamp": now,
        "paper_only": True,
        "contract": "expected_exit_bid-entry_ask-entry_fee-exit_fee-uncertainty-adverse-capital_time",
        "rows": admission_rows[:100],
    })
    base.append_csv(
        args.run_dir / "equity.csv",
        ["timestamp", "cash", "equity", "drawdown", "open_positions", "signals", "opened", "best_edge", "labeled_samples", "realized_pnl_total", "prediction_sigma_probability"],
        {
            "timestamp": now, "cash": cash, "equity": equity, "drawdown": drawdown,
            "open_positions": len(positions), "signals": signals, "opened": opened,
            "best_edge": best_edge, "labeled_samples": labeled,
            "realized_pnl_total": realized_total, "prediction_sigma_probability": sigma,
        },
    )
    print(json.dumps({
        "markets": len(markets), "labeled": labeled, "signals": signals, "opened": opened,
        "positions": len(positions), "equity": equity, "realized_pnl_total": realized_total,
        "best_edge": best_edge, "prediction_sigma_probability": sigma, "killed": killed,
        "admission_contract": "complete_round_trip_executable_ev",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
