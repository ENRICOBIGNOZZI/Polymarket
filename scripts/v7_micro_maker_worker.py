#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import v6_micro_maker as base
from v6_market_common import TapeFlow, fee_per_share, finite, resolve_fee_details
from v7_execution_core import MakerState, maker_fill_conditioned_ev, quote_improvement_is_economic


def load_tape(path: Path, cutoff: int, now: int) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle) if cutoff <= int(finite(row.get("timestamp"), 0.0)) <= now + 30]
    except OSError:
        return []


def trade_id(row: dict[str, str]) -> str:
    return "|".join(str(row.get(key) or "") for key in ("transaction_hash", "asset_id", "timestamp", "received_ms", "side", "price", "size"))


def causal_after_order(row: dict[str, str], order: dict[str, Any]) -> bool:
    event_ms = int(finite(row.get("timestamp"), 0.0) * 1000)
    received_ms = int(finite(row.get("received_ms"), 0.0))
    return event_ms > int(finite(order.get("created_event_ms"), 0.0)) and received_ms > int(finite(order.get("created_received_ms"), 0.0))


def immediate_unwind_loss(book: base.Book, limit: float, details: Any, slip: float) -> float:
    exit_price = max(1e-6, book.bid * (1.0 - slip))
    return max(0.0, limit + fee_per_share(limit, details, taker=False) - exit_price + fee_per_share(exit_price, details, taker=True))


def maker_state(
    *,
    book: base.Book,
    token: str,
    fair: float,
    limit: float,
    target_shares: float,
    details: Any,
    flow: TapeFlow,
    ttl_seconds: int,
    slippage_bps: float,
    confidence: float,
    capital_usd: float,
    capital_cost_bps_per_hour: float,
) -> MakerState:
    slip = max(0.0, slippage_bps) / 10000.0
    queue = book.touch_size(True) if abs(limit - book.bid) <= max(1e-9, 0.25 * book.tick) else 0.0
    compatible_rate = flow.compatible_sell_rate(token, limit, lookback_seconds=max(60, ttl_seconds * 5))
    compatible_volume = compatible_rate * max(1, ttl_seconds)
    bid_depth, ask_depth = book.depth(True), book.depth(False)
    imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth + 1e-9)
    ofi = flow.signed_flow(token, lookback_seconds=max(60, ttl_seconds * 5))
    future_bid = max(0.001, min(0.999, fair - 0.5 * max(0.0, book.spread)))
    exit_price = future_bid * (1.0 - slip)
    entry_fee = fee_per_share(limit, details, taker=False)
    exit_fee = fee_per_share(exit_price, details, taker=True)
    adverse = max(0.0, 0.10 * book.spread * (1.0 - confidence))
    return MakerState(
        side="BUY",
        limit_price=limit,
        fair_exit_price=exit_price,
        queue_ahead=queue,
        own_size=target_shares,
        compatible_flow=compatible_volume,
        flow_horizon_seconds=ttl_seconds,
        ofi=ofi,
        imbalance=imbalance,
        microprice=book.micro(),
        midpoint=book.mid,
        displayed_depth=max(1e-9, bid_depth + ask_depth),
        entry_fee_per_share=entry_fee,
        exit_fee_per_share=exit_fee,
        slippage_per_share=max(0.0, future_bid - exit_price),
        adverse_markout_per_share=adverse,
        partial_unwind_loss_per_share=immediate_unwind_loss(book, limit, details, slip),
        expected_partial_fraction=0.25,
        capital_usd=capital_usd,
        capital_time_rate_per_second=max(0.0, capital_cost_bps_per_hour) / 10000.0 / 3600.0,
        expected_rest_seconds=ttl_seconds,
        latency_seconds=0.1,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 fill-conditioned toxicity-aware Micro Maker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=1000)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--max-order-usd", type=float, default=125.0)
    parser.add_argument("--ttl-seconds", type=int, default=60)
    parser.add_argument("--hold-seconds", type=int, default=240)
    parser.add_argument("--flow-lookback-seconds", type=int, default=300)
    parser.add_argument("--min-fill-probability", type=float, default=0.001)
    parser.add_argument("--max-improve-ticks", type=int, default=1)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--capital-cost-bps-per-hour", type=float, default=0.25)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    starting = float(cfg["starting_capital"]); max_drawdown = float(cfg.get("max_drawdown", 0.15))
    now = int(time.time()); current_ms = time.time_ns() // 1_000_000
    args.run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"cash": starting, "peak": starting, "killed": False, "orders": {}, "positions": {}}
    cash = finite(state.get("cash"), starting); peak = max(starting, finite(state.get("peak"), starting)); killed = bool(state.get("killed"))
    orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    realized_pnl = finite(state.get("realized_pnl"), 0.0)

    try:
        markets = base.discover(gamma, args.markets, args.min_liquidity)
        books = base.fetch_books(clob, markets)
    except Exception as exc:
        markets, books = [], {}
        state["failure"] = f"market_data:{type(exc).__name__}:{exc}"
    by_id = {market.id: market for market in markets}
    flow = TapeFlow.from_csv(args.trade_tape, lookback_seconds=args.flow_lookback_seconds, now=now)
    tape = load_tape(args.trade_tape, now - max(args.flow_lookback_seconds, args.ttl_seconds + 30), now)

    order_fields = ["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence", "fill_probability", "expected_value", "toxicity_score", "flow_rate", "fee_source"]
    fill_fields = ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "pnl", "reason"]
    decision_fields = ["timestamp", "market_id", "slug", "side", "limit_price", "fill_probability", "conditional_net_pnl_per_share", "expected_value", "toxicity_score", "action"]

    # Cancel dead queues and process each public print only once across own orders.
    for market_id, order in list(orders.items()):
        created = int(finite(order.get("created_ts"), now))
        if now - created >= args.ttl_seconds:
            base.append_csv(args.run_dir / "maker_order_log.csv", order_fields, {**order, "timestamp": now, "action": "CANCEL_TTL"})
            del orders[market_id]
            continue
        token = str(order.get("token_id") or ""); limit = finite(order.get("limit_price"), 0.0)
        recent = flow.compatible_sell_volume(token, limit, lookback_seconds=min(args.flow_lookback_seconds, max(20, now - created)))
        if now - created >= 20 and recent <= 1e-12:
            base.append_csv(args.run_dir / "maker_order_log.csv", order_fields, {**order, "timestamp": now, "action": "CANCEL_ZERO_CAUSAL_FLOW"})
            del orders[market_id]

    seen_capacity: dict[str, float] = {}
    for row in tape:
        if str(row.get("side") or "").upper() != "SELL":
            continue
        identity = trade_id(row)
        total_size = max(0.0, finite(row.get("size"), 0.0)); remaining = seen_capacity.setdefault(identity, total_size)
        if remaining <= 1e-12:
            continue
        candidates = sorted(
            [(mid, order) for mid, order in orders.items() if causal_after_order(row, order) and str(order.get("token_id") or "") == str(row.get("asset_id") or "") and finite(row.get("price"), 2.0) <= finite(order.get("limit_price"), 0.0) + 1e-12],
            key=lambda item: (int(finite(item[1].get("created_received_ms"), 0.0)), item[0]),
        )
        for market_id, order in candidates:
            if remaining <= 1e-12 or market_id not in orders:
                break
            queue = max(0.0, finite(order.get("queue_ahead"), 0.0)); used = min(queue, remaining); queue -= used; remaining -= used; order["queue_ahead"] = queue
            fill = min(max(0.0, finite(order.get("remaining_shares"), 0.0)), remaining)
            if fill <= 1e-12:
                continue
            market = by_id.get(market_id)
            if market is None:
                continue
            details = resolve_fee_details(market.raw, clob, market.condition, str(order["token_id"]))
            if not details.verified:
                continue
            fee = fill * fee_per_share(float(order["limit_price"]), details, taker=False); cost = fill * float(order["limit_price"]) + fee
            if cost > cash + 1e-9:
                continue
            cash -= cost; remaining -= fill; order["remaining_shares"] = max(0.0, float(order["remaining_shares"]) - fill)
            position = positions.get(market_id) if isinstance(positions.get(market_id), dict) else {"market_id": market_id, "event_id": market.event, "slug": market.slug, "side": order["side"], "token_id": order["token_id"], "shares": 0.0, "cost": 0.0, "entry_ts": now}
            position["shares"] = finite(position.get("shares"), 0.0) + fill; position["cost"] = finite(position.get("cost"), 0.0) + cost; positions[market_id] = position
            base.append_csv(args.run_dir / "maker_fills.csv", fill_fields, {"timestamp": now, "market_id": market_id, "slug": market.slug, "action": "BUY_MAKER", "side": order["side"], "shares": fill, "price": order["limit_price"], "fee": fee, "pnl": 0.0, "reason": "dual_clock_shared_capacity_fill"})
            if order["remaining_shares"] <= 1e-12:
                del orders[market_id]
        seen_capacity[identity] = remaining

    slip = max(0.0, args.slippage_bps) / 10000.0
    for market_id, position in list(positions.items()):
        market = by_id.get(market_id); book = books.get(str(position.get("token_id") or ""))
        if market is None or book is None or not math.isfinite(book.bid):
            continue
        if now - int(finite(position.get("entry_ts"), now)) < args.hold_seconds and not killed:
            continue
        details = resolve_fee_details(market.raw, clob, market.condition, str(position["token_id"]))
        if not details.verified:
            continue
        price = max(1e-6, book.bid * (1.0 - slip)); shares = finite(position.get("shares"), 0.0); fee = shares * fee_per_share(price, details, taker=True)
        proceeds = shares * price - fee; pnl = proceeds - finite(position.get("cost"), 0.0); cash += proceeds; realized_pnl += pnl
        base.append_csv(args.run_dir / "maker_fills.csv", fill_fields, {"timestamp": now, "market_id": market_id, "slug": market.slug, "action": "SELL_TAKER", "side": position["side"], "shares": shares, "price": price, "fee": fee, "pnl": pnl, "reason": "hold_timeout" if not killed else "kill_switch"})
        del positions[market_id]

    def reserved() -> float:
        return sum(max(0.0, finite(order.get("remaining_shares"), 0.0) * finite(order.get("limit_price"), 0.0)) for order in orders.values())
    def mark() -> float:
        total = 0.0
        for position in positions.values():
            book = books.get(str(position.get("token_id") or ""))
            if book and math.isfinite(book.bid): total += finite(position.get("shares"), 0.0) * book.bid
        return total
    equity = cash + mark(); peak = max(peak, equity); drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0; killed = killed or drawdown >= max_drawdown

    signals = posted = fee_unverified = 0; best_ev = -math.inf; best_fill = 0.0
    if not killed:
        candidates: list[tuple[float, base.Market, str, base.Book, str, MakerState, Any, float]] = []
        for market in markets:
            if market.id in orders or market.id in positions:
                continue
            yes, no = books.get(market.yes), books.get(market.no)
            if yes is None or no is None or not math.isfinite(yes.mid):
                continue
            q_yes, confidence = base.micro_signal(yes, no)
            if confidence < 0.10:
                continue
            for side, book, token, fair in (("YES", yes, market.yes, q_yes), ("NO", no, market.no, 1.0 - q_yes)):
                if not math.isfinite(book.bid) or not math.isfinite(book.ask) or book.ask <= book.bid:
                    continue
                details = resolve_fee_details(market.raw, clob, market.condition, token)
                if not details.verified:
                    fee_unverified += 1; continue
                available = max(0.0, min(args.max_order_usd, cash - reserved(), float(cfg.get("max_market_fraction", 0.05)) * max(equity, 1.0)))
                if available <= 0.0:
                    continue
                shares = max(book.min_order, min(available / max(book.bid, 1e-9), 0.25 * max(1.0, book.touch_size(True))))
                touch = maker_state(book=book, token=token, fair=fair, limit=book.bid, target_shares=shares, details=details, flow=flow, ttl_seconds=args.ttl_seconds, slippage_bps=args.slippage_bps, confidence=confidence, capital_usd=shares * book.bid, capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
                decision = maker_fill_conditioned_ev(touch); chosen = touch
                if args.max_improve_ticks > 0 and book.bid + book.tick < book.ask - 1e-12:
                    improved = maker_state(book=book, token=token, fair=fair, limit=book.bid + book.tick, target_shares=shares, details=details, flow=flow, ttl_seconds=args.ttl_seconds, slippage_bps=args.slippage_bps, confidence=confidence, capital_usd=shares * (book.bid + book.tick), capital_cost_bps_per_hour=args.capital_cost_bps_per_hour)
                    if quote_improvement_is_economic(touch, improved):
                        chosen = improved; decision = maker_fill_conditioned_ev(improved)
                post_cost_edge = chosen.conditional_net_pnl_per_share
                signals += int(post_cost_edge >= args.min_edge)
                action = "POST" if decision.expected_value > 0.0 and decision.fill_probability >= args.min_fill_probability and post_cost_edge >= args.min_edge else "SKIP"
                base.append_csv(args.run_dir / "maker_decisions.csv", decision_fields, {"timestamp": now, "market_id": market.id, "slug": market.slug, "side": side, "limit_price": chosen.limit_price, "fill_probability": decision.fill_probability, "conditional_net_pnl_per_share": decision.conditional_net_pnl_per_share, "expected_value": decision.expected_value, "toxicity_score": decision.toxicity_score, "action": action})
                if action == "POST": candidates.append((decision.expected_value, market, side, book, token, chosen, details, confidence))
        candidates.sort(reverse=True, key=lambda item: item[0])
        for ev_value, market, side, book, token, chosen, details, confidence in candidates:
            if market.id in orders or market.id in positions:
                continue
            event_committed = sum(finite(order.get("remaining_shares"), 0.0) * finite(order.get("limit_price"), 0.0) for order in orders.values() if str(order.get("event_id") or "") == market.event) + sum(finite(position.get("cost"), 0.0) for position in positions.values() if str(position.get("event_id") or "") == market.event)
            room = min(args.max_order_usd, max(0.0, cash - reserved()), max(0.0, float(cfg.get("max_event_fraction", 0.15)) * max(equity, 1.0) - event_committed), max(0.0, float(cfg.get("max_gross_fraction", 0.70)) * max(equity, 1.0) - reserved()))
            shares = min(chosen.own_size, room / max(chosen.limit_price, 1e-9))
            if shares + 1e-12 < book.min_order: continue
            order = {"market_id": market.id, "event_id": market.event, "condition_id": market.condition, "slug": market.slug, "side": side, "token_id": token, "limit_price": chosen.limit_price, "remaining_shares": shares, "queue_ahead": chosen.queue_ahead, "created_ts": now, "created_event_ms": current_ms, "created_received_ms": current_ms + 100, "signal_edge": chosen.conditional_net_pnl_per_share, "confidence": confidence, "fill_probability": maker_fill_conditioned_ev(chosen).fill_probability, "expected_value": ev_value, "toxicity_score": maker_fill_conditioned_ev(chosen).toxicity_score, "flow_rate": flow.compatible_sell_rate(token, chosen.limit_price, lookback_seconds=args.flow_lookback_seconds), "fee_source": details.source}
            orders[market.id] = order; posted += 1; best_ev = max(best_ev, ev_value); best_fill = max(best_fill, float(order["fill_probability"]))
            base.append_csv(args.run_dir / "maker_order_log.csv", order_fields, {**order, "timestamp": now, "action": "POST"})

    equity = cash + mark(); peak = max(peak, equity); drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0; killed = killed or drawdown >= max_drawdown
    output = {"timestamp": now, "paper_only": True, "authenticated_execution": False, "cash": cash, "equity": equity, "peak": peak, "drawdown": drawdown, "killed": killed, "orders": orders, "positions": positions, "realized_pnl": realized_pnl, "signals": signals, "posted": posted, "resting_orders": len(orders), "open_positions": len(positions), "reserved_cash": reserved(), "best_fill_conditioned_ev": best_ev if math.isfinite(best_ev) else 0.0, "best_fill_probability": best_fill, "fee_unverified_markets": fee_unverified, "decision_contract": "fill_conditioned_net_pnl_with_toxicity_partial_unwind_and_capital_time"}
    base.atomic_json(state_path, output); base.atomic_json(args.run_dir / "status.json", output)
    base.atomic_csv(args.run_dir / "maker_orders.csv", ["market_id", "event_id", "condition_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "created_ts", "created_event_ms", "created_received_ms", "signal_edge", "confidence", "fill_probability", "expected_value", "toxicity_score", "flow_rate", "fee_source"], list(orders.values()))
    base.atomic_csv(args.run_dir / "maker_positions.csv", ["market_id", "event_id", "slug", "side", "token_id", "shares", "cost", "entry_ts"], list(positions.values()))
    base.append_csv(args.run_dir / "maker_equity.csv", ["timestamp", "cash", "equity", "reserved_cash", "resting_orders", "positions", "peak_equity", "drawdown", "killed", "realized_pnl", "signals", "posted", "best_fill_conditioned_ev", "best_fill_probability"], {"timestamp": now, "cash": cash, "equity": equity, "reserved_cash": reserved(), "resting_orders": len(orders), "positions": len(positions), "peak_equity": peak, "drawdown": drawdown, "killed": 1 if killed else 0, "realized_pnl": realized_pnl, "signals": signals, "posted": posted, "best_fill_conditioned_ev": output["best_fill_conditioned_ev"], "best_fill_probability": best_fill})
    print(json.dumps({key: output[key] for key in ("signals", "posted", "resting_orders", "open_positions", "equity", "realized_pnl", "best_fill_conditioned_ev", "best_fill_probability", "fee_unverified_markets", "killed")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
