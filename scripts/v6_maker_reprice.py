#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

try:
    import v6_micro_maker as maker
    from v6_market_common import TapeFlow, fee_per_share, fill_probability_proxy, resolve_fee_details
except ModuleNotFoundError:
    from scripts import v6_micro_maker as maker
    from scripts.v6_market_common import TapeFlow, fee_per_share, fill_probability_proxy, resolve_fee_details


def walk_asks(book: maker.Book, shares: float, slippage_bps: float, fee: Any) -> tuple[float, float, float] | None:
    remaining = max(0.0, shares)
    if remaining <= 1e-12:
        return None
    cash = 0.0
    fees = 0.0
    filled = 0.0
    slip = 1.0 + max(0.0, slippage_bps) / 10000.0
    for price, size in book.asks:
        if remaining <= 1e-12:
            break
        q = min(remaining, max(0.0, size))
        if q <= 0.0:
            continue
        px = min(0.999999, price * slip)
        cash += q * px
        fees += q * fee_per_share(px, fee, taker=True)
        filled += q
        remaining -= q
    if filled + 1e-9 < shares:
        return None
    return cash / filled, fees / filled, filled


def main() -> int:
    parser = argparse.ArgumentParser(description="Reprice/cancel stale V6 maker orders and optionally convert strong residual edge to a cost-positive taker fill")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=1000)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--taker-min-edge", type=float, default=0.00005)
    parser.add_argument("--reprice-after-seconds", type=int, default=10)
    parser.add_argument("--dead-queue-cancel-seconds", type=int, default=30)
    parser.add_argument("--max-reprices", type=int, default=5)
    parser.add_argument("--max-improve-ticks", type=int, default=8)
    parser.add_argument("--target-fill-probability", type=float, default=0.10)
    parser.add_argument("--dead-fill-probability", type=float, default=0.001)
    parser.add_argument("--flow-lookback-seconds", type=int, default=900)
    parser.add_argument("--fill-horizon-seconds", type=int, default=90)
    parser.add_argument("--hold-seconds", type=int, default=180)
    parser.add_argument("--adverse-selection-mult", type=float, default=0.15)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()

    state_path = args.run_dir / "state.json"
    if not state_path.exists():
        print(json.dumps({"status": "no_state", "repriced": 0, "cancelled": 0, "taker_fills": 0}, sort_keys=True))
        return 0
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if bool(state.get("killed", False)):
        print(json.dumps({"status": "killed", "repriced": 0, "cancelled": 0, "taker_fills": 0}, sort_keys=True))
        return 0

    now = int(time.time())
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    try:
        markets = maker.discover(gamma, args.markets, args.min_liquidity)
        books = maker.fetch_books(clob, markets)
    except Exception as exc:
        print(json.dumps({"status": "market_data_error", "error": f"{type(exc).__name__}:{exc}"}, sort_keys=True))
        return 0
    by_id = {m.id: m for m in markets}
    flow = TapeFlow.from_csv(args.trade_tape, lookback_seconds=args.flow_lookback_seconds, now=now)

    order_log = args.run_dir / "maker_order_log.csv"
    fill_log = args.run_dir / "maker_fills.csv"
    order_fields = ["timestamp", "event", "order_id", "market_id", "token_id", "side", "price", "shares", "queue_ahead", "fill_probability", "edge", "detail"]
    fill_fields = ["timestamp", "order_id", "market_id", "token_id", "side", "price", "shares", "queue_ahead_after", "cash_after"]

    repriced = cancelled = taker_fills = 0
    taker_shares = taker_notional = 0.0
    actions: list[dict[str, Any]] = []

    for oid, order in list(state.get("orders", {}).items()):
        age = now - int(order.get("created_ts", now))
        if age < max(1, args.reprice_after_seconds):
            continue
        if int(order.get("reprices", 0)) >= max(0, args.max_reprices):
            if age >= args.dead_queue_cancel_seconds:
                state["orders"].pop(oid, None)
                cancelled += 1
                maker.append_csv(order_log, order_fields, {
                    "timestamp": now, "event": "CANCEL_REPRICE_LIMIT", "order_id": oid,
                    "market_id": order.get("market_id", ""), "token_id": order.get("token_id", ""),
                    "side": order.get("side", ""), "price": order.get("limit_price", 0.0),
                    "shares": max(0.0, float(order.get("target_shares", 0.0)) - float(order.get("filled_shares", 0.0))),
                    "queue_ahead": order.get("queue_ahead", 0.0), "fill_probability": order.get("fill_probability", 0.0),
                    "edge": order.get("signal_edge", 0.0), "detail": "max_reprices_reached",
                })
            continue

        market = by_id.get(str(order.get("market_id") or ""))
        if market is None:
            continue
        yes, no = books.get(market.yes), books.get(market.no)
        if yes is None or no is None:
            continue
        side = str(order.get("side") or "YES").upper()
        book = yes if side == "YES" else no
        token = market.yes if side == "YES" else market.no
        if not (math.isfinite(book.bid) and math.isfinite(book.ask) and book.ask > book.bid):
            continue
        fee = resolve_fee_details(market.raw, clob, market.condition, token)
        if not fee.verified:
            continue

        q_yes, confidence = maker.micro_signal(yes, no)
        fair = q_yes if side == "YES" else 1.0 - q_yes
        spread = book.spread
        exit_mark = max(0.001, min(0.999, fair - 0.5 * spread))
        exit_px = exit_mark * (1.0 - max(0.0, args.slippage_bps) / 10000.0)
        exit_fee = fee_per_share(exit_px, fee, taker=True)
        adverse = max(0.0, args.adverse_selection_mult) * spread * (1.0 - confidence)
        remaining = max(0.0, float(order.get("target_shares", 0.0)) - float(order.get("filled_shares", 0.0)))
        if remaining <= 1e-9:
            state["orders"].pop(oid, None)
            continue

        # Strong-edge fallback: cross only if the same signal stays positive after
        # displayed-depth VWAP, entry/exit fees, slippage and adverse selection.
        old_reserved = remaining * max(0.0, float(order.get("limit_price", 0.0)))
        max_take = min(remaining, old_reserved / max(book.ask, 1e-9)) if old_reserved > 0 else 0.0
        max_take = min(max_take, max(0.0, float(state.get("cash", 0.0))) / max(book.ask, 1e-9))
        if max_take >= book.min_order:
            walked = walk_asks(book, max_take, args.slippage_bps, fee)
            if walked is not None:
                entry_avg, entry_fee_ps, take_shares = walked
                taker_edge = exit_px - exit_fee - entry_avg - entry_fee_ps - adverse
                total_cost = take_shares * (entry_avg + entry_fee_ps)
                if taker_edge > args.taker_min_edge and total_cost <= float(state.get("cash", 0.0)) + 1e-9:
                    state["cash"] = float(state.get("cash", 0.0)) - total_cost
                    pos = state.setdefault("positions", {}).setdefault(token, {
                        "market_id": market.id, "event_id": market.event, "side": side,
                        "shares": 0.0, "cost": 0.0, "fees": 0.0,
                        "open_ts": now, "hold_until_ts": now + args.hold_seconds,
                    })
                    pos["shares"] = float(pos.get("shares", 0.0)) + take_shares
                    pos["cost"] = float(pos.get("cost", 0.0)) + take_shares * entry_avg
                    pos["fees"] = float(pos.get("fees", 0.0)) + take_shares * entry_fee_ps
                    pos["open_ts"] = min(int(pos.get("open_ts", now)), now)
                    pos["hold_until_ts"] = max(int(pos.get("hold_until_ts", 0)), now + args.hold_seconds)
                    state["orders"].pop(oid, None)
                    taker_fills += 1
                    taker_shares += take_shares
                    taker_notional += take_shares * entry_avg
                    maker.append_csv(fill_log, fill_fields, {
                        "timestamp": now, "order_id": oid, "market_id": market.id, "token_id": token,
                        "side": side, "price": entry_avg, "shares": take_shares,
                        "queue_ahead_after": 0.0, "cash_after": state["cash"],
                    })
                    maker.append_csv(order_log, order_fields, {
                        "timestamp": now, "event": "TAKER_CONVERT", "order_id": oid, "market_id": market.id,
                        "token_id": token, "side": side, "price": entry_avg, "shares": take_shares,
                        "queue_ahead": 0.0, "fill_probability": 1.0, "edge": taker_edge,
                        "detail": "stale_maker_crossed_only_after_positive_depth_fee_slippage_edge",
                    })
                    actions.append({"order_id": oid, "action": "TAKER_CONVERT", "edge": taker_edge, "shares": take_shares})
                    continue

        current_limit = max(0.0, float(order.get("limit_price", 0.0)))
        current_queue = max(0.0, float(order.get("queue_ahead", 0.0)))
        current_rate = flow.compatible_sell_rate(token, current_limit, lookback_seconds=args.flow_lookback_seconds)
        current_fillp = fill_probability_proxy(
            queue_ahead=current_queue, own_shares=remaining,
            compatible_flow_per_second=current_rate, horizon_seconds=args.fill_horizon_seconds,
            prior_flow_per_second=1.0 / 300.0,
        )

        best: tuple[float, float, float, float] | None = None
        start = max(book.bid, current_limit)
        tick = max(1e-6, book.tick)
        for k in range(0, max(0, args.max_improve_ticks) + 1):
            price = start + k * tick
            if price >= book.ask - 1e-12:
                break
            queue = book.touch_size(True) if abs(price - book.bid) <= 0.25 * tick else 0.0
            rate = flow.compatible_sell_rate(token, price, lookback_seconds=args.flow_lookback_seconds)
            fillp = fill_probability_proxy(
                queue_ahead=queue, own_shares=remaining, compatible_flow_per_second=rate,
                horizon_seconds=args.fill_horizon_seconds, prior_flow_per_second=1.0 / 300.0,
            )
            edge = exit_px - exit_fee - price - adverse
            if edge <= args.min_edge:
                continue
            utility = edge * fillp
            candidate = (utility, price, fillp, edge)
            if best is None or candidate > best:
                best = candidate
            if fillp >= args.target_fill_probability:
                break

        if best is not None:
            _, new_price, new_fillp, new_edge = best
            if new_price > current_limit + 0.25 * tick and new_fillp > current_fillp + 1e-6:
                new_remaining = min(remaining, old_reserved / max(new_price, 1e-9)) if old_reserved > 0 else remaining
                if new_remaining >= book.min_order:
                    order["target_shares"] = float(order.get("filled_shares", 0.0)) + new_remaining
                    order["limit_price"] = new_price
                    order["initial_queue_ahead"] = book.touch_size(True) if abs(new_price - book.bid) <= 0.25 * tick else 0.0
                    order["queue_ahead"] = order["initial_queue_ahead"]
                    order["created_ts"] = now
                    order["expires_ts"] = now + max(args.reprice_after_seconds * 3, 30)
                    order["seen_trade_keys"] = []
                    order["signal_edge"] = new_edge
                    order["fill_probability"] = new_fillp
                    order["signal_confidence"] = confidence
                    order["reprices"] = int(order.get("reprices", 0)) + 1
                    repriced += 1
                    maker.append_csv(order_log, order_fields, {
                        "timestamp": now, "event": "REPRICE", "order_id": oid, "market_id": market.id,
                        "token_id": token, "side": side, "price": new_price, "shares": new_remaining,
                        "queue_ahead": order["queue_ahead"], "fill_probability": new_fillp, "edge": new_edge,
                        "detail": "priority_reset_only_when_fill_probability_improves_and_edge_remains_positive",
                    })
                    actions.append({"order_id": oid, "action": "REPRICE", "price": new_price, "fill_probability": new_fillp, "edge": new_edge})
                    continue

        if age >= args.dead_queue_cancel_seconds and current_fillp < args.dead_fill_probability:
            state["orders"].pop(oid, None)
            cancelled += 1
            maker.append_csv(order_log, order_fields, {
                "timestamp": now, "event": "CANCEL_DEAD_QUEUE", "order_id": oid, "market_id": market.id,
                "token_id": token, "side": side, "price": current_limit, "shares": remaining,
                "queue_ahead": current_queue, "fill_probability": current_fillp,
                "edge": order.get("signal_edge", 0.0), "detail": "capital_recycled_from_low_fill_probability_queue",
            })
            actions.append({"order_id": oid, "action": "CANCEL_DEAD_QUEUE", "fill_probability": current_fillp})

    maker.atomic_json(state_path, state)
    status = {
        "timestamp": now,
        "paper_only": True,
        "repriced": repriced,
        "cancelled_dead_queue": cancelled,
        "taker_conversions": taker_fills,
        "taker_shares": taker_shares,
        "taker_notional": taker_notional,
        "open_orders": len(state.get("orders", {})),
        "actions": actions[:50],
    }
    maker.atomic_json(args.run_dir / "maker_reprice_status.json", status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
