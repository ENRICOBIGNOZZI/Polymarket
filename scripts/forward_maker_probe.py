#!/usr/bin/env python3
"""Forward, read-only shadow experiment for passive Polymarket quotes.

The experiment never submits orders. It freezes several hypothetical two-sided
limit-order policies, observes *future* public books and taker prints, and then
replays conservative price-time queue fills. The output is evidence for a later
paper/live decision, not permission to trade.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from polymarket_fees import resolve_fee_details


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        x = value.strip().lower()
        if x in {"true", "1", "yes"}:
            return True
        if x in {"false", "0", "no"}:
            return False
    return default


def parse_timestamp(value: Any) -> int:
    """Return epoch seconds for numeric or ISO timestamps."""
    if isinstance(value, (int, float)):
        x = int(value)
        return x // 1000 if x > 10_000_000_000 else x
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        x = int(float(raw))
        return x // 1000 if x > 10_000_000_000 else x
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: Any | None = None,
    timeout: float = 20.0,
    retries: int = 3,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "User-Agent": "polymarket-forward-maker-probe/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt + 1 < max(1, retries):
                time.sleep(min(4.0, 0.5 * 2**attempt))
    raise RuntimeError(f"request failed after {retries} attempts: {url}: {last}")


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    token_id: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    tick_size: float
    min_order_size: float

    @property
    def best_bid(self) -> float:
        return max((x.price for x in self.bids), default=math.nan)

    @property
    def best_ask(self) -> float:
        return min((x.price for x in self.asks), default=math.nan)

    @property
    def midpoint(self) -> float:
        b, a = self.best_bid, self.best_ask
        return 0.5 * (a + b) if math.isfinite(a) and math.isfinite(b) and a >= b else math.nan


@dataclass(frozen=True)
class Trade:
    ts: int
    token_id: str
    side: str
    price: float
    size: float
    trade_id: str
    fee_rate_bps: float = 0.0


@dataclass(frozen=True)
class QuoteLeg:
    token_id: str
    outcome: str
    limit_price: float
    target_shares: float
    queue_ahead: float
    arrival_ts: float


@dataclass
class LegReplay:
    outcome: str
    token_id: str
    limit_price: float
    target_shares: float
    initial_queue_ahead: float
    compatible_sell_volume: float
    queue_remaining: float
    filled_shares: float
    first_fill_ts: int | None
    last_fill_ts: int | None
    markout_60_bid_per_share: float | None
    markout_300_bid_per_share: float | None
    final_bid: float | None
    final_mid: float | None
    fee_rate: float


def parse_levels(raw: Any) -> tuple[Level, ...]:
    out: list[Level] = []
    if not isinstance(raw, list):
        return ()
    for value in raw:
        if not isinstance(value, dict):
            continue
        price, size = finite(value.get("price"), -1.0), finite(value.get("size"), 0.0)
        if 0.0 < price < 1.0 and size > 0.0:
            out.append(Level(price, size))
    return tuple(out)


def parse_book(value: dict[str, Any]) -> Book | None:
    token = str(value.get("asset_id") or value.get("token_id") or "")
    bids, asks = parse_levels(value.get("bids")), parse_levels(value.get("asks"))
    tick = max(1e-6, finite(value.get("tick_size"), 0.01))
    minimum = max(0.0, finite(value.get("min_order_size"), 1.0))
    if not token or not bids or not asks:
        return None
    return Book(token, bids, asks, tick, minimum)


def fetch_books(clob_url: str, tokens: Iterable[str], timeout: float) -> dict[str, Book]:
    unique = list(dict.fromkeys(x for x in tokens if x))
    out: dict[str, Book] = {}
    for pos in range(0, len(unique), 100):
        chunk = unique[pos : pos + 100]
        root = request_json(
            clob_url.rstrip("/") + "/books",
            method="POST",
            body=[{"token_id": token} for token in chunk],
            timeout=timeout,
        )
        if not isinstance(root, list):
            raise RuntimeError("unexpected /books response")
        for item in root:
            if isinstance(item, dict):
                book = parse_book(item)
                if book is not None:
                    out[book.token_id] = book
    return out


def queue_at(book: Book, price: float) -> float:
    tolerance = max(1e-9, 0.25 * book.tick_size)
    return sum(x.size for x in book.bids if abs(x.price - price) <= tolerance)


def quote_price(book: Book, policy: str) -> float | None:
    bid, ask, tick = book.best_bid, book.best_ask, book.tick_size
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0.0 or ask <= bid:
        return None
    if policy == "join":
        price = bid
    elif policy == "improve1":
        price = min(bid + tick, ask - tick)
    elif policy == "fade1":
        price = max(tick, bid - tick)
    else:
        raise ValueError(f"unknown policy: {policy}")
    if not math.isfinite(price) or price <= 0.0 or price >= ask - 1e-12:
        return None
    ticks = round(price / tick)
    price = ticks * tick
    return price if 0.0 < price < ask - 1e-12 else None


def fetch_reward_tokens(clob_url: str, wanted: set[str], timeout: float) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    cursor = ""
    seen: set[str] = set()
    for _ in range(100):
        query = "?limit=500"
        if cursor:
            query += "&next_cursor=" + urllib.parse.quote(cursor, safe="")
        root = request_json(clob_url.rstrip("/") + "/rewards/markets/multi" + query, timeout=timeout)
        if not isinstance(root, dict) or not isinstance(root.get("data"), list):
            raise RuntimeError("unexpected rewards market response")
        for item in root["data"]:
            if not isinstance(item, dict):
                continue
            condition = str(item.get("condition_id") or "")
            if condition not in wanted:
                continue
            tokens: dict[str, str] = {}
            for token in item.get("tokens") or []:
                if not isinstance(token, dict):
                    continue
                outcome = str(token.get("outcome") or "").upper()
                token_id = str(token.get("token_id") or "")
                if outcome in {"YES", "NO"} and token_id:
                    tokens[outcome] = token_id
            if "YES" in tokens and "NO" in tokens:
                out[condition] = tokens
        if wanted.issubset(out):
            break
        nxt = str(root.get("next_cursor") or "")
        if not nxt or nxt == "LTE=" or nxt == cursor or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    return out


def gamma_eligible(gamma_url: str, market_id: str, end_ts: int, timeout: float) -> bool:
    root = request_json(gamma_url.rstrip("/") + "/markets/" + urllib.parse.quote(market_id, safe=""), timeout=timeout)
    if not isinstance(root, dict):
        return False
    if not as_bool(root.get("active"), True) or as_bool(root.get("closed"), False):
        return False
    if not as_bool(root.get("enableOrderBook"), True) or not as_bool(root.get("acceptingOrders"), True):
        return False
    candidates: list[dict[str, Any]] = [root]
    events = root.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        candidates.append(events[0])
    start = 0
    timed = False
    for obj in candidates:
        start = start or parse_timestamp(obj.get("gameStartTime"))
        timed = timed or bool(str(obj.get("sportsMarketType") or "").strip()) or start > 0
    return not timed or start > end_ts + 60


def fetch_trades(
    data_url: str,
    condition_id: str,
    start_ts: int,
    end_ts: int,
    timeout: float,
) -> list[Trade]:
    query = urllib.parse.urlencode(
        {
            "market": condition_id,
            "limit": 10000,
            "takerOnly": "true",
            "start": start_ts,
            "end": end_ts,
        }
    )
    root = request_json(data_url.rstrip("/") + "/trades?" + query, timeout=timeout)
    values = root if isinstance(root, list) else root.get("data", []) if isinstance(root, dict) else []
    out: dict[str, Trade] = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        token = str(item.get("asset") or item.get("asset_id") or "")
        side = str(item.get("side") or "").upper()
        price, size = finite(item.get("price"), -1.0), finite(item.get("size"), 0.0)
        ts = parse_timestamp(item.get("timestamp"))
        tx = str(item.get("transactionHash") or item.get("transaction_hash") or "")
        fee_bps = max(0.0, finite(item.get("fee_rate_bps"), 0.0))
        if not token or ts <= 0 or side not in {"BUY", "SELL"} or not (0.0 < price < 1.0) or size <= 0.0:
            continue
        key = tx or f"{token}:{ts}:{side}:{price:.12g}:{size:.12g}"
        out[key] = Trade(ts, token, side, price, size, key, fee_bps)
    return sorted(out.values(), key=lambda x: (x.ts, x.trade_id))


def snapshot_after(
    snapshots: list[tuple[int, dict[str, Book]]],
    token: str,
    target_ts: int,
) -> Book | None:
    for ts, books in snapshots:
        if ts >= target_ts and token in books:
            return books[token]
    return snapshots[-1][1].get(token) if snapshots else None


def simulate_leg(
    leg: QuoteLeg,
    trades: Iterable[Trade],
    snapshots: list[tuple[int, dict[str, Book]]],
    fee_rate: float = 0.0,
) -> LegReplay:
    queue = max(0.0, leg.queue_ahead)
    remaining = max(0.0, leg.target_shares)
    compatible = 0.0
    first: int | None = None
    last: int | None = None
    for trade in sorted(trades, key=lambda x: (x.ts, x.trade_id)):
        if trade.token_id != leg.token_id or trade.side != "SELL":
            continue
        if trade.ts + 1e-9 < leg.arrival_ts or trade.price > leg.limit_price + 1e-12:
            continue
        compatible += trade.size
        consume = trade.size
        q = min(queue, consume)
        queue -= q
        consume -= q
        if consume <= 1e-12 or remaining <= 1e-12:
            continue
        fill = min(remaining, consume)
        remaining -= fill
        if fill > 1e-12:
            first = trade.ts if first is None else first
            last = trade.ts
        if remaining <= 1e-12:
            break
    filled = max(0.0, leg.target_shares - remaining)
    b60 = snapshot_after(snapshots, leg.token_id, (first or 0) + 60) if first is not None else None
    b300 = snapshot_after(snapshots, leg.token_id, (first or 0) + 300) if first is not None else None
    final = snapshots[-1][1].get(leg.token_id) if snapshots else None

    def mark(book: Book | None) -> float | None:
        return book.best_bid - leg.limit_price if book is not None and math.isfinite(book.best_bid) else None

    return LegReplay(
        outcome=leg.outcome,
        token_id=leg.token_id,
        limit_price=leg.limit_price,
        target_shares=leg.target_shares,
        initial_queue_ahead=leg.queue_ahead,
        compatible_sell_volume=compatible,
        queue_remaining=queue,
        filled_shares=filled,
        first_fill_ts=first,
        last_fill_ts=last,
        markout_60_bid_per_share=mark(b60),
        markout_300_bid_per_share=mark(b300),
        final_bid=final.best_bid if final is not None and math.isfinite(final.best_bid) else None,
        final_mid=final.midpoint if final is not None and math.isfinite(final.midpoint) else None,
        fee_rate=max(0.0, fee_rate),
    )


def protocol_fee(shares: float, price: float, rate: float) -> float:
    if shares <= 0.0 or not (0.0 < price < 1.0) or rate <= 0.0:
        return 0.0
    return shares * rate * price * (1.0 - price)


def policy_result(
    *,
    base: dict[str, Any],
    policy: str,
    yes: LegReplay,
    no: LegReplay,
    quote_start_ts: int,
    quote_end_ts: int,
    exit_slippage_bps: float,
    eligible_fraction: float,
) -> dict[str, Any]:
    matched = min(yes.filled_shares, no.filled_shares)
    unmatched_yes = max(0.0, yes.filled_shares - matched)
    unmatched_no = max(0.0, no.filled_shares - matched)
    locked_edge = 1.0 - yes.limit_price - no.limit_price
    locked_pnl = matched * locked_edge

    slip = max(0.0, exit_slippage_bps) / 10000.0
    liquidation_pnl = 0.0
    exit_fees = 0.0
    for leg, unmatched in ((yes, unmatched_yes), (no, unmatched_no)):
        if unmatched <= 0.0:
            continue
        final_bid = leg.final_bid if leg.final_bid is not None else 0.0
        exit_price = max(0.0, final_bid * (1.0 - slip))
        proceeds = unmatched * exit_price
        fee = protocol_fee(unmatched, exit_price, leg.fee_rate)
        liquidation_pnl += proceeds - fee - unmatched * leg.limit_price
        exit_fees += fee

    active_seconds = max(0, quote_end_ts - quote_start_ts)
    estimated_daily = max(0.0, finite(base.get("estimated_native_daily_value")))
    reward_seconds = active_seconds * max(0.0, min(1.0, eligible_fraction))
    prorated_reward = estimated_daily * reward_seconds / 86400.0
    conservative = locked_pnl + liquidation_pnl
    pair_delay = None
    if yes.first_fill_ts is not None and no.first_fill_ts is not None:
        pair_delay = abs(yes.first_fill_ts - no.first_fill_ts)

    rebate_basis = (
        protocol_fee(yes.filled_shares, yes.limit_price, yes.fee_rate)
        + protocol_fee(no.filled_shares, no.limit_price, no.fee_rate)
    )

    return {
        "market_id": str(base.get("market_id") or ""),
        "condition_id": str(base.get("condition_id") or ""),
        "event_id": str(base.get("event_id") or ""),
        "slug": str(base.get("slug") or ""),
        "question": str(base.get("question") or ""),
        "policy": policy,
        "quote_start_ts": quote_start_ts,
        "quote_end_ts": quote_end_ts,
        "active_seconds": active_seconds,
        "reward_eligible_fraction_approx": eligible_fraction,
        "reward_eligible_seconds_approx": reward_seconds,
        "yes": asdict(yes),
        "no": asdict(no),
        "yes_quote": yes.limit_price,
        "no_quote": no.limit_price,
        "quote_sum": yes.limit_price + no.limit_price,
        "locked_edge_per_matched_share": locked_edge,
        "matched_shares": matched,
        "unmatched_yes_shares": unmatched_yes,
        "unmatched_no_shares": unmatched_no,
        "any_fill": yes.filled_shares > 0.0 or no.filled_shares > 0.0,
        "pair_fill": matched > 0.0,
        "one_sided_only": (yes.filled_shares > 0.0) ^ (no.filled_shares > 0.0),
        "pair_completion_delay_seconds": pair_delay,
        "locked_gross_pnl_usd": locked_pnl,
        "unmatched_liquidation_pnl_usd": liquidation_pnl,
        "exit_fees_usd": exit_fees,
        "conservative_pnl_ex_rewards_usd": conservative,
        "conditional_prorated_reward_usd": prorated_reward,
        "conditional_pnl_including_reward_usd": conservative + prorated_reward,
        "maker_rebate_fee_basis_usd_not_revenue": rebate_basis,
        "source_locked_complete_set_edge": finite(base.get("locked_complete_set_edge")),
        "source_conditional_daily_reward_score": finite(base.get("conditional_conservative_daily_score")),
        "source_market_competitiveness": finite(base.get("market_competitiveness")),
        "source_volume24h": finite(base.get("volume24h")),
    }


def reward_eligibility_fraction(
    yes_limit: float,
    no_limit: float,
    max_spread_cents: float,
    snapshots: list[tuple[int, dict[str, Book]]],
    yes_token: str,
    no_token: str,
) -> float:
    max_distance = max(0.0, max_spread_cents) / 100.0
    if max_distance <= 0.0 or not snapshots:
        return 0.0
    eligible = 0
    observed = 0
    for _, books in snapshots:
        y, n = books.get(yes_token), books.get(no_token)
        if y is None or n is None:
            continue
        observed += 1
        ym, nm = y.midpoint, n.midpoint
        if not math.isfinite(ym) or not math.isfinite(nm):
            continue
        noncrossing = yes_limit < y.best_ask - 1e-12 and no_limit < n.best_ask - 1e-12
        within = abs(yes_limit - ym) < max_distance and abs(no_limit - nm) < max_distance
        if noncrossing and within:
            eligible += 1
    return eligible / observed if observed else 0.0


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_policy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_policy[str(result.get("policy") or "UNKNOWN")].append(result)
    output: dict[str, Any] = {}
    for policy, rows in sorted(by_policy.items()):
        n = len(rows)
        pnl = [finite(x.get("conservative_pnl_ex_rewards_usd")) for x in rows]
        with_reward = [finite(x.get("conditional_pnl_including_reward_usd")) for x in rows]
        output[policy] = {
            "probes": n,
            "any_fill_rate": sum(bool(x.get("any_fill")) for x in rows) / n if n else 0.0,
            "pair_fill_rate": sum(bool(x.get("pair_fill")) for x in rows) / n if n else 0.0,
            "one_sided_only_rate": sum(bool(x.get("one_sided_only")) for x in rows) / n if n else 0.0,
            "matched_shares": sum(finite(x.get("matched_shares")) for x in rows),
            "conservative_pnl_ex_rewards_usd": sum(pnl),
            "conditional_pnl_including_reward_usd": sum(with_reward),
            "mean_pnl_ex_rewards_usd": statistics.fmean(pnl) if pnl else 0.0,
            "positive_pnl_rate_ex_rewards": sum(x > 0.0 for x in pnl) / n if n else 0.0,
            "mean_reward_eligible_fraction_approx": statistics.fmean(
                finite(x.get("reward_eligible_fraction_approx")) for x in rows
            ) if rows else 0.0,
        }
    return output


def load_candidates(path: Path, limit: int, seed: int) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    valid = [
        row for row in rows
        if row.get("condition_id") and row.get("market_id")
        and finite(row.get("quote_shares")) > 0.0
        and finite(row.get("locked_complete_set_edge"), -1.0) >= 0.0
    ]

    def score(row: dict[str, str]) -> float:
        locked = max(0.0, finite(row.get("locked_complete_set_edge"))) * max(0.0, finite(row.get("quote_shares")))
        reward = max(0.0, finite(row.get("estimated_native_daily_value")))
        competition = max(0.0, finite(row.get("market_competitiveness")))
        return locked + 0.25 * reward + 0.01 / (1.0 + competition)

    valid.sort(key=lambda row: (score(row), finite(row.get("volume24h"))), reverse=True)
    selected: list[dict[str, str]] = []
    seen_events: set[str] = set()
    for row in valid:
        event = row.get("event_id") or row.get("condition_id") or ""
        if event in seen_events:
            continue
        selected.append(row)
        seen_events.add(event)
        if len(selected) >= limit:
            return selected
    for row in valid:
        if row in selected:
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    random.Random(seed).shuffle(selected)
    return selected


def write_csv(path: Path, results: list[dict[str, Any]]) -> None:
    fields = [
        "market_id", "condition_id", "event_id", "slug", "policy",
        "yes_quote", "no_quote", "quote_sum", "locked_edge_per_matched_share",
        "matched_shares", "unmatched_yes_shares", "unmatched_no_shares",
        "any_fill", "pair_fill", "one_sided_only", "pair_completion_delay_seconds",
        "reward_eligible_fraction_approx", "locked_gross_pnl_usd",
        "unmatched_liquidation_pnl_usd", "conservative_pnl_ex_rewards_usd",
        "conditional_prorated_reward_usd", "conditional_pnl_including_reward_usd",
        "maker_rebate_fee_basis_usd_not_revenue",
        "yes_filled_shares", "no_filled_shares",
        "yes_initial_queue", "no_initial_queue",
        "yes_compatible_sell_volume", "no_compatible_sell_volume",
        "yes_markout_60", "no_markout_60", "yes_markout_300", "no_markout_300",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            yes, no = result["yes"], result["no"]
            flat = {key: result.get(key) for key in fields}
            flat.update({
                "yes_filled_shares": yes.get("filled_shares"),
                "no_filled_shares": no.get("filled_shares"),
                "yes_initial_queue": yes.get("initial_queue_ahead"),
                "no_initial_queue": no.get("initial_queue_ahead"),
                "yes_compatible_sell_volume": yes.get("compatible_sell_volume"),
                "no_compatible_sell_volume": no.get("compatible_sell_volume"),
                "yes_markout_60": yes.get("markout_60_bid_per_share"),
                "no_markout_60": no.get("markout_60_bid_per_share"),
                "yes_markout_300": yes.get("markout_300_bid_per_share"),
                "no_markout_300": no.get("markout_300_bid_per_share"),
            })
            writer.writerow(flat)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--opportunities", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--data-url", default="https://data-api.polymarket.com")
    parser.add_argument("--markets", type=int, default=12)
    parser.add_argument("--duration-seconds", type=int, default=360)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--arrival-latency-ms", type=int, default=250)
    parser.add_argument("--max-quote-shares", type=float, default=50.0)
    parser.add_argument("--max-notional", type=float, default=100.0)
    parser.add_argument("--exit-slippage-bps", type=float, default=10.0)
    parser.add_argument("--policies", default="join,improve1,fade1")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args()

    if args.markets <= 0 or args.duration_seconds < 1 or args.poll_seconds <= 0.0:
        raise SystemExit("markets, duration and poll interval must be positive")
    policies = [x.strip() for x in args.policies.split(",") if x.strip()]
    if not policies or any(x not in {"join", "improve1", "fade1"} for x in policies):
        raise SystemExit("policies must be drawn from join,improve1,fade1")

    source = load_candidates(args.opportunities, args.markets * 3, args.seed)
    end_plan = int(time.time()) + args.duration_seconds
    eligible: list[dict[str, str]] = []
    for row in source:
        try:
            if gamma_eligible(args.gamma_url, str(row.get("market_id") or ""), end_plan, args.timeout_seconds):
                eligible.append(row)
        except RuntimeError:
            continue
        if len(eligible) >= args.markets:
            break

    conditions = {str(x.get("condition_id") or "") for x in eligible}
    token_map = fetch_reward_tokens(args.clob_url, conditions, args.timeout_seconds) if conditions else {}
    eligible = [row for row in eligible if str(row.get("condition_id") or "") in token_map]
    tokens = [
        token_map[str(row["condition_id"])][outcome]
        for row in eligible
        for outcome in ("YES", "NO")
    ]
    initial_books = fetch_books(args.clob_url, tokens, args.timeout_seconds) if tokens else {}
    quote_start = int(time.time())
    snapshots: list[tuple[int, dict[str, Book]]] = [(quote_start, initial_books)]

    definitions: list[tuple[dict[str, str], str, QuoteLeg, QuoteLeg]] = []
    for row in eligible:
        condition = str(row["condition_id"])
        ytoken, ntoken = token_map[condition]["YES"], token_map[condition]["NO"]
        ybook, nbook = initial_books.get(ytoken), initial_books.get(ntoken)
        if ybook is None or nbook is None:
            continue
        for policy in policies:
            yprice, nprice = quote_price(ybook, policy), quote_price(nbook, policy)
            if yprice is None or nprice is None:
                continue
            minimum = max(
                finite(row.get("rewards_min_size")),
                ybook.min_order_size,
                nbook.min_order_size,
            )
            desired = max(minimum, min(args.max_quote_shares, finite(row.get("quote_shares"), minimum)))
            capital_limited = args.max_notional / (yprice + nprice) if args.max_notional > 0.0 else desired
            shares = min(desired, capital_limited)
            if shares + 1e-9 < minimum:
                continue
            arrival = quote_start + max(0, args.arrival_latency_ms) / 1000.0
            definitions.append((
                row,
                policy,
                QuoteLeg(ytoken, "YES", yprice, shares, queue_at(ybook, yprice), arrival),
                QuoteLeg(ntoken, "NO", nprice, shares, queue_at(nbook, nprice), arrival),
            ))

    deadline = time.monotonic() + args.duration_seconds
    while definitions and time.monotonic() < deadline:
        sleep_for = min(args.poll_seconds, max(0.0, deadline - time.monotonic()))
        if sleep_for > 0.0:
            time.sleep(sleep_for)
        try:
            books = fetch_books(args.clob_url, tokens, args.timeout_seconds)
            snapshots.append((int(time.time()), books))
        except RuntimeError:
            continue
    quote_end = int(time.time())

    trades_by_condition: dict[str, list[Trade]] = {}
    for row in eligible:
        condition = str(row["condition_id"])
        try:
            trades_by_condition[condition] = fetch_trades(
                args.data_url, condition, quote_start - 1, quote_end + 1, args.timeout_seconds
            )
        except RuntimeError:
            trades_by_condition[condition] = []

    fee_by_condition = {}
    for condition in {str(row["condition_id"]) for row in eligible}:
        try:
            fee_by_condition[condition] = resolve_fee_details(
                {"conditionId": condition},
                args.clob_url,
                lambda url, *_args: request_json(url, timeout=args.timeout_seconds),
            )
        except Exception:
            pass
    results: list[dict[str, Any]] = []
    for row, policy, yleg, nleg in definitions:
        condition = str(row["condition_id"])
        fee = fee_by_condition.get(condition)
        if fee is None:
            continue
        trades = trades_by_condition.get(condition, [])
        yes = simulate_leg(yleg, trades, snapshots, fee.rate)
        no = simulate_leg(nleg, trades, snapshots, fee.rate)
        eligibility = reward_eligibility_fraction(
            yleg.limit_price,
            nleg.limit_price,
            finite(row.get("rewards_max_spread_cents")),
            snapshots,
            yleg.token_id,
            nleg.token_id,
        )
        results.append(policy_result(
            base=row,
            policy=policy,
            yes=yes,
            no=no,
            quote_start_ts=quote_start,
            quote_end_ts=quote_end,
            exit_slippage_bps=args.exit_slippage_bps,
            eligible_fraction=eligibility,
        ))

    payload = {
        "schema": "polymarket_forward_maker_probe_v1",
        "generated_ts": int(time.time()),
        "read_only": True,
        "submitted_orders": 0,
        "method": {
            "queue_model": "trade-print conservative FIFO; cancellations do not reduce queue",
            "counterfactual_policies": policies,
            "duration_seconds_requested": args.duration_seconds,
            "poll_seconds": args.poll_seconds,
            "arrival_latency_ms": args.arrival_latency_ms,
            "exit_slippage_bps": args.exit_slippage_bps,
            "reward_eligibility": "approximate fixed-quote uptime; actual venue scoring not claimed",
            "maker_rebate": "fee-generation basis only; no rebate revenue is booked",
            "market_impact": "ignored; policy comparisons are shadow counterfactuals",
        },
        "source_candidates": len(source),
        "eligible_markets": len(eligible),
        "quote_definitions": len(definitions),
        "book_snapshots": len(snapshots),
        "quote_start_ts": quote_start,
        "quote_end_ts": quote_end,
        "aggregate_by_policy": aggregate(results),
        "results": results,
    }
    atomic_json(args.output, payload)
    if args.csv is not None:
        write_csv(args.csv, results)
    print(
        "forward_maker_probe"
        f" source_candidates={len(source)}"
        f" eligible_markets={len(eligible)}"
        f" quote_definitions={len(definitions)}"
        f" snapshots={len(snapshots)}"
        f" any_fills={sum(bool(x.get('any_fill')) for x in results)}"
        f" pair_fills={sum(bool(x.get('pair_fill')) for x in results)}"
        f" one_sided={sum(bool(x.get('one_sided_only')) for x in results)}"
        f" pnl_ex_rewards={sum(finite(x.get('conservative_pnl_ex_rewards_usd')) for x in results):.12g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
