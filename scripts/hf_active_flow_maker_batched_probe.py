#!/usr/bin/env python3
from __future__ import annotations

import json
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any

import hf_active_flow_maker_core as core


def fetch_trades_batch(condition_ids: list[str], start_ts: int, end_ts: int,
                       batch_size: int = 20) -> tuple[dict[str, list[core.Trade]], int, list[str]]:
    out: dict[str, list[core.Trade]] = defaultdict(list)
    last_received_ms = 0
    errors: list[str] = []
    unique_conditions = list(dict.fromkeys(x for x in condition_ids if x))
    for lo in range(0, len(unique_conditions), batch_size):
        chunk = unique_conditions[lo:lo + batch_size]
        market_value = ",".join(chunk)
        seen: set[str] = set()
        for offset in (0, 10000):
            query = (
                f"limit=10000&offset={offset}&takerOnly=true&start={max(0,start_ts)}&end={max(0,end_ts)}"
                f"&market={urllib.parse.quote(market_value, safe=',')}"
            )
            try:
                raw, received_ms = core.request_json(f"{core.DATA_URL}/trades?{query}")
                last_received_ms = max(last_received_ms, received_ms)
            except Exception as exc:
                errors.append(f"batch={lo // batch_size}:{type(exc).__name__}:{exc}")
                break
            rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
            if not isinstance(rows, list):
                errors.append(f"batch={lo // batch_size}:unexpected_response")
                break
            for item in rows:
                if not isinstance(item, dict):
                    continue
                condition = str(item.get("conditionId") or "")
                token = str(item.get("asset") or "")
                side = str(item.get("side") or "").upper()
                ts = int(core.number(item.get("timestamp"), 0))
                price = core.number(item.get("price"), -1.0)
                size = core.number(item.get("size"), 0.0)
                if condition not in chunk or not token or ts <= 0 or not (0 < price < 1) or size <= 0:
                    continue
                key = ":".join([str(item.get("transactionHash") or ""), token, str(ts), side,
                                f"{price:.12g}", f"{size:.12g}"])
                if key in seen:
                    continue
                seen.add(key)
                out[condition].append(core.Trade(key, token, side, price, size, ts))
            if len(rows) < 10000:
                break
        for condition in chunk:
            out[condition].sort(key=lambda x: (x.ts, x.trade_id))
    return dict(out), last_received_ms, errors


def run_batched(args: Any) -> dict[str, Any]:
    started = core.now_s()
    markets = core.discover_markets(args.markets, args.min_liquidity)
    conditions = [m.condition_id for m in markets]

    query_lookback = max(args.recent_lookback_seconds + args.trade_index_lag_seconds, 300)
    query_end = core.now_s()
    flows, flow_received_ms, flow_errors = fetch_trades_batch(
        conditions, query_end - query_lookback, query_end, args.trade_batch_size)

    active_markets = [m for m in markets if flows.get(m.condition_id)]
    active_markets.sort(key=lambda m: (
        max((t.ts for t in flows.get(m.condition_id, [])), default=0), m.volume24h, m.liquidity
    ), reverse=True)
    active_markets = active_markets[:args.activity_scan_markets]
    active_ids = {m.market_id for m in active_markets}

    books = core.fetch_books([token for m in active_markets for token in (m.yes_token, m.no_token)]) if active_markets else {}
    decision_ts = core.now_s()
    baseline, active = core.build_candidates(active_markets, books, flows, decision_ts, active_ids, args)
    selected = {
        "baseline_active_universe": core.select_with_caps(baseline, args.max_orders_per_arm, args),
        "active_flow": core.select_with_caps(active, args.max_orders_per_arm, args),
    }
    for c in selected["baseline_active_universe"]:
        c.arm = "baseline_active_universe"

    orders = [core.ShadowOrder(c, decision_ts, core.now_ms(), decision_ts + args.order_ttl_seconds,
                               c.shares, c.queue_ahead)
              for candidates in selected.values() for c in candidates]
    outcomes: dict[tuple[str, str, str], core.FillOutcome] = {}
    poll_errors: list[str] = []
    markout_errors = 0
    max_horizon = max(args.markout_seconds)
    end_wall = (decision_ts + args.order_ttl_seconds + args.trade_index_lag_seconds +
                max_horizon + args.markout_buffer_seconds)

    while True:
        current = core.now_s()
        live_orders = [o for o in orders if o.remaining > 1e-12 and
                       current <= o.expires_ts + args.trade_index_lag_seconds]
        if live_orders:
            live_conditions = list(dict.fromkeys(o.candidate.market.condition_id for o in live_orders))
            replay, received_ms, errors = fetch_trades_batch(
                live_conditions, decision_ts,
                min(current, decision_ts + args.order_ttl_seconds),
                args.trade_batch_size)
            poll_errors.extend(errors)
            received_ts = received_ms // 1000 if received_ms else current
            for order in live_orders:
                core.consume(order, replay.get(order.candidate.market.condition_id, []), received_ts)
                if order.filled <= 0 or order.first_fill_event_ts is None or order.first_fill_received_ts is None:
                    continue
                key = (order.candidate.arm, order.candidate.market.market_id, order.candidate.token_id)
                existing = outcomes.get(key)
                if existing is None:
                    outcomes[key] = core.FillOutcome(
                        order.candidate.arm, order.candidate.market.market_id, order.candidate.side,
                        order.candidate.token_id, order.filled, order.candidate.limit_price,
                        order.first_fill_event_ts, order.first_fill_received_ts)
                else:
                    existing.shares = order.filled

        filled_tokens = list({o.token_id for o in outcomes.values()})
        fresh: dict[str, core.Book] = {}
        if filled_tokens:
            try:
                fresh = core.fetch_books(filled_tokens)
            except Exception:
                markout_errors += 1
        for outcome in outcomes.values():
            book = fresh.get(outcome.token_id)
            if not book:
                continue
            elapsed = current - outcome.first_fill_received_ts
            for horizon in args.markout_seconds:
                h = str(horizon)
                if elapsed < horizon or h in outcome.markouts:
                    continue
                px = book.executable_bid(outcome.shares, args.slippage_bps)
                outcome.markouts[h] = {
                    "observed_ts": current,
                    "receive_time_horizon_seconds": elapsed,
                    "executable_bid": px,
                    "pnl_per_share_pre_fee": None if px is None else px - outcome.entry_price,
                }
                if horizon == args.exit_horizon_seconds and outcome.realized_pnl is None and px is not None:
                    market = next((o.candidate.market for o in orders if o.candidate.arm == outcome.arm and
                                   o.candidate.market.market_id == outcome.market_id), None)
                    if market and market.fee:
                        fee = core.fee_per_share(px, market.fee) * outcome.shares
                        outcome.realized_exit_price = px
                        outcome.exit_fee = fee
                        outcome.realized_pnl = outcome.shares * (px - outcome.entry_price) - fee
        if current >= end_wall:
            break
        time.sleep(max(1, args.poll_seconds))

    arms: dict[str, Any] = {}
    for arm, candidates in selected.items():
        arm_orders = [o for o in orders if o.candidate.arm == arm]
        arm_outcomes = [x for x in outcomes.values() if x.arm == arm]
        closed = [x for x in arm_outcomes if x.realized_pnl is not None]
        closed_shares = sum(x.shares for x in closed)
        pnl = sum(float(x.realized_pnl) for x in closed)
        arms[arm] = {
            "candidates_available": len(baseline if arm == "baseline_active_universe" else active),
            "orders": len(arm_orders),
            "fill_orders": sum(o.filled > 0 for o in arm_orders),
            "filled_shares": sum(o.filled for o in arm_orders),
            "fill_rate": sum(o.filled > 0 for o in arm_orders) / len(arm_orders) if arm_orders else 0.0,
            "realized_round_trips": len(closed),
            "closed_shares": closed_shares,
            "fill_conditioned_pnl": pnl,
            "fill_conditioned_pnl_per_share": pnl / closed_shares if closed_shares > 0 else None,
            "orders_detail": [core.candidate_dict(o.candidate) | {
                "filled_shares": o.filled,
                "remaining_shares": o.remaining,
                "queue_remaining": o.queue_ahead,
                "first_fill_event_ts": o.first_fill_event_ts,
                "first_fill_received_ts": o.first_fill_received_ts,
            } for o in arm_orders],
            "outcomes": [x.__dict__ for x in arm_outcomes],
        }

    return {
        "schema": "hf_active_flow_maker_batched_probe_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_money_execution": False,
        "generated_ts": core.now_s(),
        "run_started_ts": started,
        "decision_ts": decision_ts,
        "universe": {
            "requested_markets": args.markets,
            "discovered_markets": len(markets),
            "conditions_with_indexed_trades": len(active_ids),
            "active_markets_evaluated": len(active_markets),
            "book_count": len(books),
            "flow_query_received_ms": flow_received_ms,
            "flow_errors": flow_errors[:20],
        },
        "method": {
            "batched_condition_queries": True,
            "query_lookback_seconds": query_lookback,
            "feature_lookback_seconds": args.recent_lookback_seconds,
            "max_event_age_seconds": args.max_event_age_seconds,
            "min_recent_trades": args.min_recent_trades,
            "min_sell_prints": args.min_sell_prints,
            "min_fill_probability": args.min_fill_probability,
            "max_sell_toxicity": args.max_sell_toxicity,
            "order_ttl_seconds": args.order_ttl_seconds,
            "trade_index_lag_seconds": args.trade_index_lag_seconds,
            "markout_seconds": args.markout_seconds,
            "markout_clock": "local_receive_time_after_causal_fill_discovery",
            "counterfactual_arms_are_independent": True,
            "fee_provenance_fail_closed": True,
        },
        "safety": {
            "max_drawdown": args.max_drawdown,
            "max_order_usd": args.max_order_usd,
            "max_market_fraction": args.max_market_fraction,
            "max_event_fraction": args.max_event_fraction,
            "max_gross_fraction": args.max_gross_fraction,
        },
        "arms": arms,
        "poll_errors": poll_errors[:20],
        "markout_book_errors": markout_errors,
    }


def markdown(result: dict[str, Any]) -> str:
    lines = ["# HF batched active-flow maker probe", "",
             "| Arm | Orders | Fill orders | Filled shares | Closed PnL | PnL/share |",
             "|---|---:|---:|---:|---:|---:|"]
    for arm, data in result["arms"].items():
        pps = data["fill_conditioned_pnl_per_share"]
        lines.append(f"| {arm} | {data['orders']} | {data['fill_orders']} | {data['filled_shares']:.6f} | {data['fill_conditioned_pnl']:.6f} | {'' if pps is None else f'{pps:.8f}'} |")
    lines += ["", f"- discovered: {result['universe']['discovered_markets']} markets",
              f"- conditions with indexed public trades: {result['universe']['conditions_with_indexed_trades']}",
              f"- active books evaluated: {result['universe']['active_markets_evaluated']}",
              "- no authenticated orders are submitted; fills are FIFO public-tape counterfactuals."]
    return "\n".join(lines) + "\n"


def main() -> int:
    args = core.parse_args()
    args.trade_batch_size = 20
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = run_batched(args)
    (out / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "result.md").write_text(markdown(result), encoding="utf-8")
    print(markdown(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
