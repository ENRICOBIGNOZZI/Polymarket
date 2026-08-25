#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _levels(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in book.get(side, []) or []:
        if not isinstance(row, dict):
            continue
        price = _f(row.get("price"), math.nan)
        size = _f(row.get("size"), 0.0)
        if math.isfinite(price) and price > 0.0 and size > 0.0:
            out.append((price, size))
    return out


def _tick(book: dict[str, Any]) -> float:
    tick = _f(book.get("tick_size", book.get("tickSize")), math.nan)
    return tick if math.isfinite(tick) and tick > 0.0 else math.nan


def _best_bid(book: dict[str, Any]) -> float:
    bids = _levels(book, "bids")
    return max((p for p, _ in bids), default=math.nan)


def _best_ask(book: dict[str, Any]) -> float:
    asks = _levels(book, "asks")
    return min((p for p, _ in asks), default=math.nan)


def _size_at(book: dict[str, Any], side: str, price: float, tick: float) -> float:
    tol = max(1e-12, 0.1 * tick)
    return sum(size for px, size in _levels(book, side) if abs(px - price) <= tol)


def fetch_book(clob_url: str, token_id: str, timeout: float = 8.0) -> dict[str, Any]:
    query = urllib.parse.urlencode({"token_id": token_id})
    req = urllib.request.Request(
        f"{clob_url.rstrip('/')}/book?{query}",
        headers={"User-Agent": "polymarket-v6-queue-shadow/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("CLOB book response is not an object")
    return data


def latest_post_metadata(path: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            action = (row.get("action") or "").upper()
            if action not in {"POST", "REPRICE_INSIDE_SHADOW"}:
                continue
            market_id = row.get("market_id") or ""
            if not market_id:
                continue
            out[market_id] = {
                "edge": _f(row.get("signal_edge"), math.nan),
                "confidence": _f(row.get("confidence"), 0.0),
            }
    return out


def decide(
    order: dict[str, str],
    signal_edge: float,
    book: dict[str, Any],
    *,
    min_edge: float,
    max_queue_ratio: float,
    max_improve_ticks: int,
) -> dict[str, Any]:
    old_price = _f(order.get("limit_price"), math.nan)
    shares = _f(order.get("remaining_shares"), 0.0)
    queue_ahead = _f(order.get("queue_ahead"), 0.0)
    tick = _tick(book)
    best_bid = _best_bid(book)
    best_ask = _best_ask(book)
    min_order = _f(book.get("min_order_size", book.get("minOrderSize")), 0.0)
    ratio = queue_ahead / max(shares, 1e-12)
    result: dict[str, Any] = {
        "market_id": order.get("market_id", ""),
        "token_id": order.get("token_id", ""),
        "old_price": old_price,
        "old_shares": shares,
        "queue_ahead": queue_ahead,
        "queue_ratio": ratio,
        "signal_edge": signal_edge,
        "tick_size": tick if math.isfinite(tick) else None,
        "best_bid": best_bid if math.isfinite(best_bid) else None,
        "best_ask": best_ask if math.isfinite(best_ask) else None,
        "action": "SKIP_INVALID_BOOK",
        "new_price": old_price,
        "new_shares": shares,
        "new_queue_ahead": queue_ahead,
        "edge_after_price": signal_edge,
        "improve_ticks": 0,
    }
    if not (math.isfinite(old_price) and old_price > 0 and shares > 0):
        return result
    if not (math.isfinite(tick) and math.isfinite(best_bid) and math.isfinite(best_ask) and best_ask > best_bid):
        return result

    # If the public touch has already moved above our stored quote, the incumbent
    # would cancel it as stale on the next tick. Do not manufacture a reprice.
    if best_bid > old_price + 0.5 * tick:
        result["action"] = "CANCEL_STALE_SHADOW"
        return result

    if ratio <= max_queue_ratio:
        result["action"] = "KEEP_JOIN"
        return result

    if not math.isfinite(signal_edge):
        result["action"] = "CANCEL_DEAD_QUEUE_SHADOW"
        return result

    affordable = max(0, int(math.floor((signal_edge - min_edge + 1e-12) / tick)))
    inside = 0
    for n in range(1, max(0, max_improve_ticks) + 1):
        candidate = old_price + n * tick
        if candidate < best_ask - 0.25 * tick:
            inside = n
        else:
            break
    improve = min(max_improve_ticks, affordable, inside)
    if improve <= 0:
        result["action"] = "CANCEL_DEAD_QUEUE_SHADOW"
        return result

    new_price = old_price + improve * tick
    edge_after = signal_edge - (new_price - old_price)
    original_notional = old_price * shares
    new_shares = min(shares, original_notional / max(new_price, 1e-12))
    if min_order > 0.0 and new_shares + 1e-12 < min_order:
        result["action"] = "CANCEL_DEAD_QUEUE_SHADOW"
        return result

    result.update(
        {
            "action": "REPRICE_INSIDE_SHADOW",
            "new_price": new_price,
            "new_shares": new_shares,
            "new_queue_ahead": _size_at(book, "bids", new_price, tick),
            "edge_after_price": edge_after,
            "improve_ticks": improve,
        }
    )
    return result


def _write_orders(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def apply_shadow_plan(run_dir: Path, decisions: list[dict[str, Any]], now: int) -> None:
    orders_path = run_dir / "maker_orders.csv"
    log_path = run_dir / "maker_order_log.csv"
    with orders_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    by_market = {d["market_id"]: d for d in decisions}
    kept: list[dict[str, str]] = []
    log_rows: list[list[Any]] = []
    metadata = latest_post_metadata(log_path)
    for row in rows:
        market_id = row.get("market_id") or ""
        d = by_market.get(market_id)
        if not d:
            kept.append(row)
            continue
        action = d["action"]
        meta = metadata.get(market_id, {})
        confidence = meta.get("confidence", 0.0)
        if action == "REPRICE_INSIDE_SHADOW":
            row["limit_price"] = f"{float(d['new_price']):.12g}"
            row["remaining_shares"] = f"{float(d['new_shares']):.12g}"
            row["queue_ahead"] = f"{float(d['new_queue_ahead']):.12g}"
            # Reprice time is the causal queue-entry time. Reset the tape cursor so
            # pre-reprice trades cannot be counted as fills at the improved price.
            row["created_ts"] = str(now)
            row["last_trade_ts"] = str(now)
            row["last_trade_keys"] = "|"
            kept.append(row)
            log_rows.append(
                [now, action, market_id, row.get("slug", ""), row.get("side", ""), row.get("token_id", ""),
                 row["limit_price"], row["remaining_shares"], row["queue_ahead"],
                 f"{float(d['edge_after_price']):.12g}", f"{confidence:.12g}"]
            )
        elif action in {"CANCEL_DEAD_QUEUE_SHADOW", "CANCEL_STALE_SHADOW"}:
            log_rows.append(
                [now, action, market_id, row.get("slug", ""), row.get("side", ""), row.get("token_id", ""),
                 row.get("limit_price", ""), row.get("remaining_shares", ""), row.get("queue_ahead", ""),
                 f"{float(d.get('edge_after_price') or 0.0):.12g}", f"{confidence:.12g}"]
            )
        else:
            kept.append(row)
    _write_orders(orders_path, fieldnames, kept)
    if log_rows:
        with log_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerows(log_rows)


def summarize_fills(run_dir: Path) -> dict[str, Any]:
    fills_path = run_dir / "maker_fills.csv"
    if not fills_path.exists():
        return {"fills": 0, "buy_fills": 0, "sell_fills": 0}
    with fills_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    buy = sum((r.get("action") or "").upper().startswith("BUY") for r in rows)
    sell = sum((r.get("action") or "").upper().startswith("SELL") for r in rows)
    return {"fills": len(rows), "buy_fills": buy, "sell_fills": sell}


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir)
    orders_path = run_dir / "maker_orders.csv"
    if not orders_path.exists():
        raise FileNotFoundError(orders_path)
    with orders_path.open(newline="", encoding="utf-8") as fh:
        orders = list(csv.DictReader(fh))
    metadata = latest_post_metadata(run_dir / "maker_order_log.csv")
    decisions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for order in orders:
        market_id = order.get("market_id") or ""
        token_id = order.get("token_id") or ""
        try:
            book = fetch_book(args.clob_url, token_id, args.timeout)
            edge = metadata.get(market_id, {}).get("edge", math.nan)
            decisions.append(
                decide(
                    order,
                    edge,
                    book,
                    min_edge=args.min_edge,
                    max_queue_ratio=args.max_queue_ratio,
                    max_improve_ticks=args.max_improve_ticks,
                )
            )
        except Exception as exc:
            failures.append({"market_id": market_id, "error": f"{type(exc).__name__}:{exc}"})
    now = int(time.time())
    if args.apply and decisions:
        apply_shadow_plan(run_dir, decisions, now)
    ratios = [float(d["queue_ratio"]) for d in decisions if math.isfinite(float(d["queue_ratio"]))]
    actions = Counter(str(d["action"]) for d in decisions)
    summary = {
        "schema": "polymarket_v6_micro_maker_queue_shadow_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "timestamp": now,
        "orders_seen": len(orders),
        "books_evaluated": len(decisions),
        "book_failures": failures,
        "max_queue_ratio": args.max_queue_ratio,
        "max_improve_ticks": args.max_improve_ticks,
        "min_edge": args.min_edge,
        "queue_ratio_min": min(ratios) if ratios else None,
        "queue_ratio_median": sorted(ratios)[len(ratios) // 2] if ratios else None,
        "queue_ratio_max": max(ratios) if ratios else None,
        "orders_above_queue_cap": sum(r > args.max_queue_ratio for r in ratios),
        "actions": dict(actions),
        "fills": summarize_fills(run_dir),
        "decisions": decisions,
        "applied": bool(args.apply),
    }
    if args.output:
        Path(args.output).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue-aware, paper-only V6 micro-maker shadow repricer")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--min-edge", type=float, default=0.0002)
    parser.add_argument("--max-queue-ratio", type=float, default=50.0)
    parser.add_argument("--max-improve-ticks", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    summary = run(args)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
