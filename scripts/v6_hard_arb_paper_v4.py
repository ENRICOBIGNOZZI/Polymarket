#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import v6_hard_arb_paper_v2 as base
    from v6_market_common import FeeDetails, fee_per_share, finite, request_json, resolve_fee_details
except ModuleNotFoundError:
    from scripts import v6_hard_arb_paper_v2 as base
    from scripts.v6_market_common import FeeDetails, fee_per_share, finite, request_json, resolve_fee_details


@dataclass
class DepthBook:
    token: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    min_order: float
    tick: float
    received_ms: int
    stable: bool = False

    @property
    def ask_depth(self) -> float:
        return sum(q for _p, q in self.asks)

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else math.nan

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else math.nan


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def _fetch_one(clob: str, token: str) -> DepthBook | None:
    try:
        raw = request_json(clob.rstrip("/") + "/book?" + urllib.parse.urlencode({"token_id": token}))
    except Exception:
        return None
    received = time.monotonic_ns() // 1_000_000
    if not isinstance(raw, dict):
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, output in (("bids", bids), ("asks", asks)):
        for row in raw.get(key, []):
            if not isinstance(row, dict):
                continue
            price, size = finite(row.get("price"), math.nan), finite(row.get("size"), 0.0)
            if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
                output.append((price, size))
    bids.sort(reverse=True)
    asks.sort()
    if not bids or not asks:
        return None
    return DepthBook(
        token=token,
        bids=bids,
        asks=asks,
        min_order=max(1.0, finite(raw.get("min_order_size"), 1.0)),
        tick=max(1e-6, finite(raw.get("tick_size"), 0.01)),
        received_ms=received,
    )


def _snapshot(clob: str, tokens: list[str]) -> dict[str, DepthBook]:
    output: dict[str, DepthBook] = {}
    workers = min(32, max(1, len(tokens)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, clob, token): token for token in tokens}
        for future in as_completed(futures):
            token = futures[future]
            try:
                book = future.result()
            except Exception:
                book = None
            if book is not None:
                output[token] = book
    return output


def fresh_stable_books(
    clob: str,
    tokens: list[str],
    *,
    max_leg_age_ms: int = 2000,
    max_cross_leg_skew_ms: int = 1000,
    snapshot_pause_seconds: float = 0.05,
) -> dict[str, DepthBook]:
    if not tokens:
        return {}
    first = _snapshot(clob, tokens)
    time.sleep(max(0.0, snapshot_pause_seconds))
    second = _snapshot(clob, tokens)
    if any(token not in first or token not in second for token in tokens):
        return {}
    received = [second[token].received_ms for token in tokens]
    now_ms = time.monotonic_ns() // 1_000_000
    if now_ms - min(received) > max_leg_age_ms or max(received) - min(received) > max_cross_leg_skew_ms:
        return {}
    for token in tokens:
        a, b = first[token], second[token]
        tick = max(a.tick, b.tick)
        ask_ok = abs(a.best_ask - b.best_ask) <= tick + 1e-12
        bid_ok = abs(a.best_bid - b.best_bid) <= tick + 1e-12
        b.stable = ask_ok and bid_ok
        if not b.stable:
            return {}
    return second


def buy_cost(book: DepthBook, shares: float, slippage_bps: float, fee: FeeDetails) -> tuple[float, float, float] | None:
    remaining = max(0.0, shares)
    if remaining <= 0.0:
        return 0.0, 0.0, 0.0
    slip = max(0.0, slippage_bps) / 10000.0
    raw_cash = fee_cash = 0.0
    for price, depth in book.asks:
        size = min(remaining, depth)
        if size <= 0.0:
            continue
        executed = min(0.999999, price * (1.0 + slip))
        raw_cash += size * executed
        fee_cash += size * fee_per_share(executed, fee, taker=True)
        remaining -= size
        if remaining <= 1e-9:
            break
    if remaining > 1e-8:
        return None
    return raw_cash + fee_cash, raw_cash / shares, fee_cash


def sell_proceeds(book: DepthBook, shares: float, slippage_bps: float, fee: FeeDetails) -> tuple[float, float, float, float]:
    remaining = max(0.0, shares)
    slip = max(0.0, slippage_bps) / 10000.0
    raw_cash = fee_cash = sold = 0.0
    for price, depth in book.bids:
        size = min(remaining, depth)
        if size <= 0.0:
            continue
        executed = max(0.000001, price * (1.0 - slip))
        raw_cash += size * executed
        fee_cash += size * fee_per_share(executed, fee, taker=True)
        sold += size
        remaining -= size
        if remaining <= 1e-9:
            break
    proceeds = raw_cash - fee_cash
    return sold, proceeds, (raw_cash / sold if sold > 0.0 else 0.0), fee_cash


def candidate_size(
    books: list[DepthBook],
    fees: list[FeeDetails],
    *,
    cash_room: float,
    max_trade_usd: float,
    min_edge: float,
    slippage_bps: float,
) -> tuple[float, float, float] | None:
    min_order = max(book.min_order for book in books)
    max_depth = min(book.ask_depth for book in books)
    room = min(max(0.0, cash_room), max(0.0, max_trade_usd))
    if max_depth + 1e-12 < min_order or room <= 0.0:
        return None

    def economics(shares: float) -> tuple[float, float] | None:
        cost = 0.0
        for book, fee in zip(books, fees):
            item = buy_cost(book, shares, slippage_bps, fee)
            if item is None:
                return None
            cost += item[0]
        return cost, 1.0 - cost / max(shares, 1e-12)

    low = min_order
    first = economics(low)
    if first is None or first[0] > room + 1e-9 or first[1] <= min_edge:
        return None
    high = max_depth
    top = economics(high)
    if top is not None and top[0] <= room + 1e-9 and top[1] > min_edge:
        return high, top[1], top[0]
    best = (low, first[1], first[0])
    for _ in range(36):
        mid = 0.5 * (low + high)
        item = economics(mid)
        feasible = item is not None and item[0] <= room + 1e-9 and item[1] > min_edge
        if feasible:
            best = (mid, item[1], item[0])
            low = mid
        else:
            high = mid
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description="V6 sequential hard-arbitrage PAPER executor")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=700)
    parser.add_argument("--min-liquidity", type=float, default=10.0)
    parser.add_argument("--max-events", type=int, default=80)
    parser.add_argument("--min-edge", type=float, default=0.0002)
    parser.add_argument("--max-trade-usd", type=float, default=60.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--max-leg-age-ms", type=int, default=2000)
    parser.add_argument("--max-cross-leg-skew-ms", type=int, default=1000)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    starting = float(cfg["starting_capital"])
    max_drawdown = float(cfg.get("max_drawdown", 0.15))
    max_gross = float(cfg.get("max_gross_fraction", 0.45))
    max_event = float(cfg.get("max_event_fraction", 0.08))
    now = int(time.time())
    args.run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "cash": starting, "peak": starting, "killed": False, "bundles": {}, "aborting": {}, "realized_pnl": 0.0,
    }
    cash = finite(state.get("cash"), starting)
    peak = max(starting, finite(state.get("peak"), starting))
    open_bundles = state.get("bundles") if isinstance(state.get("bundles"), dict) else {}
    aborting = state.get("aborting") if isinstance(state.get("aborting"), dict) else {}
    realized = finite(state.get("realized_pnl"), 0.0)
    killed = bool(state.get("killed", False))
    failures: list[str] = []
    fields = ["timestamp", "event_id", "action", "token_id", "shares", "price", "cost", "proceeds", "fees", "pnl", "reason"]

    def persist() -> None:
        payload = {
            "timestamp": int(time.time()), "cash": cash, "peak": peak, "killed": killed,
            "bundles": open_bundles, "aborting": aborting, "realized_pnl": realized,
            "paper_only": True, "atomic_snapshot_assumption": False,
            "sequential_legging_unwind_model": True,
            "max_leg_age_ms": args.max_leg_age_ms,
            "max_cross_leg_skew_ms": args.max_cross_leg_skew_ms,
        }
        base.atomic_json(state_path, payload)

    # Resolution of fully completed guaranteed baskets.
    for event_id, bundle in list(open_bundles.items()):
        try:
            event = request_json(f"{gamma.rstrip('/')}/events/{event_id}")
            if isinstance(event, dict) and event.get("closed"):
                payout = float(bundle["shares"])
                pnl = payout - float(bundle["cost"])
                cash += payout
                realized += pnl
                append_csv(args.run_dir / "fills.csv", fields, {
                    "timestamp": now, "event_id": event_id, "action": "SETTLE_COMPLETE_SET", "shares": bundle["shares"],
                    "proceeds": payout, "cost": bundle["cost"], "pnl": pnl, "reason": "event_closed",
                })
                del open_bundles[event_id]
        except Exception as exc:
            failures.append(f"settle:{event_id}:{type(exc).__name__}")

    # Any process crash or failed leg sequence leaves an explicit aborting state.
    # Always try to flatten it before considering a new hard-arb entry.
    for event_id, item in list(aborting.items()):
        exposures = item.get("exposures") if isinstance(item.get("exposures"), list) else []
        remaining_exposures = []
        for exposure in exposures:
            token = str(exposure.get("token_id") or "")
            shares = max(0.0, finite(exposure.get("shares"), 0.0))
            total_cost = max(0.0, finite(exposure.get("cost"), 0.0))
            fee_data = exposure.get("fee") if isinstance(exposure.get("fee"), dict) else {}
            fee = FeeDetails(
                max(0.0, finite(fee_data.get("rate"), 0.07)), max(0.0, finite(fee_data.get("exponent"), 1.0)),
                bool(fee_data.get("taker_only", True)), bool(fee_data.get("verified", False)), str(fee_data.get("source") or "persisted"),
            )
            books = fresh_stable_books(clob, [token], max_leg_age_ms=args.max_leg_age_ms, max_cross_leg_skew_ms=args.max_cross_leg_skew_ms)
            book = books.get(token)
            if shares <= 1e-12:
                continue
            if book is None:
                remaining_exposures.append(exposure)
                continue
            sold, proceeds, avg_px, fee_cash = sell_proceeds(book, shares, args.slippage_bps, fee)
            if sold <= 1e-12:
                remaining_exposures.append(exposure)
                continue
            sold_cost = total_cost * sold / shares
            pnl = proceeds - sold_cost
            cash += proceeds
            realized += pnl
            append_csv(args.run_dir / "fills.csv", fields, {
                "timestamp": now, "event_id": event_id, "action": "ABORT_UNWIND", "token_id": token,
                "shares": sold, "price": avg_px, "cost": sold_cost, "proceeds": proceeds, "fees": fee_cash, "pnl": pnl,
                "reason": str(item.get("reason") or "retry_abort"),
            })
            residual = shares - sold
            if residual > 1e-9:
                remaining = dict(exposure)
                remaining["shares"] = residual
                remaining["cost"] = max(0.0, total_cost - sold_cost)
                remaining_exposures.append(remaining)
        if remaining_exposures:
            item["exposures"] = remaining_exposures
            aborting[event_id] = item
        else:
            del aborting[event_id]
    persist()

    def residual_cost() -> float:
        return sum(max(0.0, finite(exp.get("cost"), 0.0)) for item in aborting.values() for exp in (item.get("exposures") or []))

    locked_cost = sum(max(0.0, finite(bundle.get("cost"), 0.0)) for bundle in open_bundles.values())
    equity = cash + locked_cost + residual_cost()
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = killed or drawdown >= max_drawdown
    scanned = candidates = entered = aborted = 0
    best_edge = 0.0

    # Fail closed while an unwind is unresolved.
    if not killed and not aborting:
        try:
            event_ids = base.discover_event_ids(gamma, args.markets, args.min_liquidity, args.max_events)
        except Exception as exc:
            event_ids = []
            failures.append(f"discover:{type(exc).__name__}:{exc}")

        for event_id in event_ids:
            if event_id in open_bundles or event_id in aborting:
                continue
            try:
                markets = base.event_spec(gamma, event_id)
                if markets is None:
                    continue
                tokens = [(base.market_tokens(raw) or ("", ""))[0] for raw in markets]
                if any(not token for token in tokens):
                    continue
                fees: list[FeeDetails] = []
                verified = True
                for raw, token in zip(markets, tokens):
                    fee = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), token)
                    fees.append(fee)
                    verified = verified and fee.verified
                if not verified:
                    continue
                books_map = fresh_stable_books(
                    clob, tokens, max_leg_age_ms=args.max_leg_age_ms, max_cross_leg_skew_ms=args.max_cross_leg_skew_ms,
                )
                if any(token not in books_map for token in tokens):
                    continue
                scanned += 1
                eq = max(1.0, equity)
                room = min(
                    args.max_trade_usd,
                    max(0.0, max_gross * eq - locked_cost - residual_cost()),
                    max(0.0, max_event * eq),
                    cash,
                )
                size = candidate_size(
                    [books_map[token] for token in tokens], fees,
                    cash_room=room, max_trade_usd=args.max_trade_usd,
                    min_edge=args.min_edge, slippage_bps=args.slippage_bps,
                )
                if size is None:
                    continue
                shares, initial_edge, _projected_cost = size
                candidates += 1
                best_edge = max(best_edge, initial_edge)

                acquired: list[dict[str, Any]] = []
                spent = 0.0
                aborting[event_id] = {"reason": "inflight", "opened_ts": int(time.time()), "exposures": acquired}
                persist()
                failed_reason = ""
                for i, (token, fee) in enumerate(zip(tokens, fees)):
                    remaining_tokens = tokens[i:]
                    remaining_fees = fees[i:]
                    current_books = fresh_stable_books(
                        clob, remaining_tokens,
                        max_leg_age_ms=args.max_leg_age_ms,
                        max_cross_leg_skew_ms=args.max_cross_leg_skew_ms,
                    )
                    if any(t not in current_books for t in remaining_tokens):
                        failed_reason = "freshness_or_snapshot_stability"
                        break
                    projected_remaining = 0.0
                    projection_ok = True
                    for rem_token, rem_fee in zip(remaining_tokens, remaining_fees):
                        projected = buy_cost(current_books[rem_token], shares, args.slippage_bps, rem_fee)
                        if projected is None:
                            projection_ok = False
                            break
                        projected_remaining += projected[0]
                    if not projection_ok:
                        failed_reason = "remaining_depth"
                        break
                    guaranteed_edge = 1.0 - (spent + projected_remaining) / max(shares, 1e-12)
                    best_edge = max(best_edge, guaranteed_edge)
                    if guaranteed_edge <= args.min_edge:
                        failed_reason = "edge_revalidation"
                        break
                    execution = buy_cost(current_books[token], shares, args.slippage_bps, fee)
                    if execution is None or execution[0] > cash + 1e-9:
                        failed_reason = "current_leg_execution"
                        break
                    cost, avg_px, fee_cash = execution
                    cash -= cost
                    spent += cost
                    exposure = {
                        "token_id": token, "shares": shares, "cost": cost,
                        "fee": {"rate": fee.rate, "exponent": fee.exponent, "taker_only": fee.taker_only, "verified": fee.verified, "source": fee.source},
                    }
                    acquired.append(exposure)
                    aborting[event_id]["exposures"] = acquired
                    persist()
                    append_csv(args.run_dir / "fills.csv", fields, {
                        "timestamp": int(time.time()), "event_id": event_id, "action": "BUY_LEG", "token_id": token,
                        "shares": shares, "price": avg_px, "cost": cost, "fees": fee_cash, "pnl": 0.0,
                        "reason": f"leg_{i+1}_of_{len(tokens)}",
                    })

                if failed_reason:
                    aborted += 1
                    aborting[event_id]["reason"] = failed_reason
                    # Immediate unwind attempt; any unfilled residual remains persisted.
                    exposures = list(aborting[event_id]["exposures"])
                    remaining_exposures = []
                    for exposure in exposures:
                        token = str(exposure["token_id"])
                        fee_data = exposure["fee"]
                        fee = FeeDetails(float(fee_data["rate"]), float(fee_data["exponent"]), bool(fee_data["taker_only"]), bool(fee_data["verified"]), str(fee_data["source"]))
                        current = fresh_stable_books(clob, [token], max_leg_age_ms=args.max_leg_age_ms, max_cross_leg_skew_ms=args.max_cross_leg_skew_ms)
                        book = current.get(token)
                        if book is None:
                            remaining_exposures.append(exposure)
                            continue
                        sold, proceeds, avg_px, fee_cash = sell_proceeds(book, float(exposure["shares"]), args.slippage_bps, fee)
                        if sold <= 1e-12:
                            remaining_exposures.append(exposure)
                            continue
                        sold_cost = float(exposure["cost"]) * sold / float(exposure["shares"])
                        pnl = proceeds - sold_cost
                        cash += proceeds
                        realized += pnl
                        append_csv(args.run_dir / "fills.csv", fields, {
                            "timestamp": int(time.time()), "event_id": event_id, "action": "ABORT_UNWIND", "token_id": token,
                            "shares": sold, "price": avg_px, "cost": sold_cost, "proceeds": proceeds, "fees": fee_cash, "pnl": pnl,
                            "reason": failed_reason,
                        })
                        residual = float(exposure["shares"]) - sold
                        if residual > 1e-9:
                            left = dict(exposure)
                            left["shares"] = residual
                            left["cost"] = max(0.0, float(exposure["cost"]) - sold_cost)
                            remaining_exposures.append(left)
                    if remaining_exposures:
                        aborting[event_id]["exposures"] = remaining_exposures
                    else:
                        del aborting[event_id]
                    persist()
                    if aborting:
                        break
                    continue

                final_edge = 1.0 - spent / max(shares, 1e-12)
                if final_edge <= args.min_edge:
                    # This should be prevented by per-leg revalidation, but never book
                    # a completed set as arbitrage if final accounting says otherwise.
                    aborting[event_id]["reason"] = "final_edge_accounting"
                    persist()
                    aborted += 1
                    break
                del aborting[event_id]
                open_bundles[event_id] = {
                    "shares": shares, "cost": spent, "net_edge": final_edge,
                    "opened_ts": int(time.time()), "legs": len(tokens), "execution": "sequential_revalidated_vwap",
                }
                locked_cost += spent
                entered += 1
                append_csv(args.run_dir / "fills.csv", fields, {
                    "timestamp": int(time.time()), "event_id": event_id, "action": "COMPLETE_SET", "shares": shares,
                    "cost": spent, "pnl": 0.0, "reason": "all_legs_revalidated_and_filled",
                })
                persist()
            except Exception as exc:
                if len(failures) < 30:
                    failures.append(f"event:{event_id}:{type(exc).__name__}:{exc}")

    locked_cost = sum(max(0.0, finite(bundle.get("cost"), 0.0)) for bundle in open_bundles.values())
    residual = residual_cost()
    equity = cash + locked_cost + residual
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = killed or drawdown >= max_drawdown
    status = {
        "timestamp": int(time.time()), "cash": cash, "equity": equity, "peak": peak, "drawdown": drawdown,
        "killed": killed, "bundles": open_bundles, "aborting": aborting, "realized_pnl": realized,
        "gross_exposure": locked_cost + residual, "open_positions": len(open_bundles), "aborting_events": len(aborting),
        "scanned_events": scanned, "positive_candidates": candidates, "entered": entered, "aborted": aborted,
        "best_edge": best_edge, "failures": failures[:30], "paper_only": True,
        "atomic_snapshot_assumption": False, "sequential_legging_unwind_model": True,
        "book_costing": "multi_level_vwap_per_leg_with_full_remaining_revalidation",
        "cross_leg_freshness": {"max_leg_age_ms": args.max_leg_age_ms, "max_cross_leg_skew_ms": args.max_cross_leg_skew_ms},
        "crash_recovery": "every_acquired_leg_persisted_as_aborting_until_completion",
    }
    base.atomic_json(state_path, status)
    base.atomic_json(args.run_dir / "status.json", status)
    append_csv(args.run_dir / "equity.csv", [
        "timestamp", "cash", "equity", "drawdown", "gross_exposure", "open_positions", "aborting_events",
        "realized_pnl", "best_edge", "entered", "aborted", "killed",
    ], status)
    print(json.dumps({k: status[k] for k in (
        "scanned_events", "positive_candidates", "entered", "aborted", "aborting_events", "best_edge", "realized_pnl", "killed"
    )}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
