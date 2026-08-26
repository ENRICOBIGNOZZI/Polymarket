#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from v6_market_common import fee_per_share, finite, parse_array, request_json, resolve_fee_details


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def receive_time_active(received_ms: int, arrival_ms: int, cancel_effective_ms: int = 0) -> bool:
    """Return whether locally observed flow belongs to the order's causal live window.

    Exchange/event timestamps are intentionally not used for order causality: the public
    trade feed can be delayed or second-granularity.  Missing receive time fails closed.
    """
    if received_ms <= 0 or received_ms < arrival_ms:
        return False
    return cancel_effective_ms <= 0 or received_ms < cancel_effective_ms


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in rows])
    os.replace(tmp, path)


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError:
        return []


def market_event_id(raw: dict[str, Any]) -> str:
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        value = str(events[0].get("id") or "")
        if value:
            return value
    return str(raw.get("eventId") or raw.get("event_id") or "")


def side_token(raw: dict[str, Any], side: str) -> str:
    ids = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(value).strip().upper() for value in parse_array(raw.get("outcomes"))]
    for index, name in enumerate(outcomes[: len(ids)]):
        if name == side.upper():
            return ids[index]
    if len(ids) >= 2:
        return ids[0] if side.upper() == "YES" else ids[1]
    return ""


def resolved_yes(raw: dict[str, Any]) -> int | None:
    for key in ("resolvedYes", "resolved_yes", "outcome"):
        value = raw.get(key)
        if value in (0, 1, "0", "1"):
            return int(value)
    outcomes = [str(value).strip().upper() for value in parse_array(raw.get("outcomes"))]
    prices = [finite(value) for value in parse_array(raw.get("outcomePrices"))]
    if len(outcomes) == len(prices) and outcomes:
        for name, price in zip(outcomes, prices):
            if abs(price - 1.0) <= 1e-9 and name == "YES":
                return 1
            if abs(price - 1.0) <= 1e-9 and name == "NO":
                return 0
    return None


def is_closed(raw: dict[str, Any]) -> bool:
    return bool(raw.get("closed", False)) or bool(raw.get("resolved", False))


@dataclass
class Book:
    token: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    tick: float
    min_order: float
    received_ms: int

    @property
    def bid(self) -> float:
        return self.bids[0][0] if self.bids else math.nan

    @property
    def ask(self) -> float:
        return self.asks[0][0] if self.asks else math.nan

    def queue_at(self, price: float) -> float:
        return sum(size for px, size in self.bids if abs(px - price) <= max(1e-9, 0.25 * self.tick))


def parse_book(raw: dict[str, Any], received: int) -> Book | None:
    token = str(raw.get("asset_id") or "")
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, output in (("bids", bids), ("asks", asks)):
        for row in raw.get(key, []):
            if not isinstance(row, dict):
                continue
            price, size = finite(row.get("price")), max(0.0, finite(row.get("size"), 0.0))
            if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
                output.append((price, size))
    bids.sort(reverse=True)
    asks.sort()
    if not token or not bids or not asks:
        return None
    return Book(token, bids, asks, max(1e-6, finite(raw.get("tick_size"), 0.01)), max(1.0, finite(raw.get("min_order_size"), 1.0)), received)


def sell_vwap(book: Book, shares: float, slippage_bps: float) -> tuple[float, float] | None:
    remaining = max(0.0, shares)
    cash = 0.0
    sold = 0.0
    for price, size in book.bids:
        quantity = min(remaining, size)
        cash += quantity * price
        sold += quantity
        remaining -= quantity
        if remaining <= 1e-9:
            break
    if sold + 1e-9 < shares or sold <= 0.0:
        return None
    raw = cash / sold
    return raw, max(1e-6, raw * (1.0 - max(0.0, slippage_bps) / 10000.0))


@dataclass
class Leg:
    bundle_id: str
    market_id: str
    risk_event_id: str
    side: str
    token_id: str
    weight: float
    target_shares: float
    filled_shares: float
    limit_price: float
    queue_ahead: float
    arrival_ms: int
    arrival_event_ms: int
    cancel_effective_ms: int = 0
    entry_cash: float = 0.0
    entry_fee: float = 0.0
    exit_cash: float = 0.0
    exit_fee: float = 0.0
    slippage_cost: float = 0.0
    first_fill_ts: int = 0
    last_fill_ts: int = 0
    adverse_mark_pnl: float = 0.0
    adverse_recorded: bool = False
    order_state: str = "RESTING"
    exited: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, self.target_shares - self.filled_shares)

    @property
    def fill_fraction(self) -> float:
        return min(1.0, self.filled_shares / self.target_shares) if self.target_shares > 1e-12 else 0.0

    @property
    def entry_avg(self) -> float:
        return self.entry_cash / self.filled_shares if self.filled_shares > 1e-12 else 0.0


@dataclass
class Bundle:
    bundle_id: str
    strategy: str
    event_id: str
    status: str
    created_ts: int
    expected_edge: float
    max_notional: float
    execution_deadline_ts: int
    hold_deadline_ts: int
    abort_reason: str = ""
    ledger_written: bool = False


class Broker:
    BUNDLE_FIELDS = ["bundle_id", "strategy", "event_id", "status", "created_ts", "expected_edge", "max_notional", "execution_deadline_ts", "hold_deadline_ts", "ledger_written", "abort_reason"]
    LEG_FIELDS = ["bundle_id", "market_id", "event_id", "side", "token_id", "weight", "target_shares", "filled_shares", "limit_price", "queue_ahead", "arrival_ms", "arrival_event_ms", "cancel_effective_ms", "replace_count", "entry_cash", "entry_fee", "exit_cash", "exit_fee", "slippage_cost", "first_fill_ts", "last_fill_ts", "adverse_mark_pnl", "adverse_recorded", "order_state", "max_limit_price", "exited"]
    EVENT_FIELDS = ["timestamp", "event", "bundle_id", "market_id", "side", "shares", "price", "queue_ahead", "detail"]
    LEDGER_FIELDS = ["bundle_id", "strategy", "event_id", "created_ts", "closed_ts", "status", "expected_edge", "max_notional", "entry_cash", "gross_pnl", "fees", "slippage", "net_pnl", "return_on_capital", "fill_fraction", "adverse_mark_pnl", "abort_reason"]
    EQUITY_FIELDS = ["timestamp", "cash", "equity", "reserved_cash", "gross_entry_cash", "peak_equity", "drawdown", "killed", "live_bundles"]

    def __init__(self, config: Path, run_dir: Path, intents: Path, trade_tape: Path, min_edge: float, submit_latency_ms: int, slippage_bps: float, adverse_horizon_seconds: int) -> None:
        self.cfg = json.loads(config.read_text(encoding="utf-8"))
        self.gamma = str(self.cfg["gamma_url"]).rstrip("/")
        self.clob = str(self.cfg["clob_url"]).rstrip("/")
        self.run_dir = run_dir
        self.intents = intents
        self.trade_tape = trade_tape
        self.min_edge = float(min_edge)
        self.submit_latency_ms = max(0, int(submit_latency_ms))
        self.slippage_bps = max(0.0, float(slippage_bps))
        self.adverse_horizon_seconds = max(1, int(adverse_horizon_seconds))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cash = float(self.cfg["starting_capital"])
        self.peak = self.cash
        self.killed = False
        self.tape_cursor = 0
        self.bundles: dict[str, Bundle] = {}
        self.legs: list[Leg] = []
        self.market_cache: dict[str, dict[str, Any]] = {}
        self.market_cache_ts: dict[str, int] = {}
        self.load_state()
        self.ensure_logs()

    def ensure_logs(self) -> None:
        for name, fields in (("multileg_events.csv", self.EVENT_FIELDS), ("bundle_ledger.csv", self.LEDGER_FIELDS), ("multileg_equity.csv", self.EQUITY_FIELDS)):
            path = self.run_dir / name
            if not path.exists() or path.stat().st_size == 0:
                atomic_csv(path, fields, [])

    def event(self, name: str, bundle_id: str, leg: Leg | None = None, shares: float = 0.0, price: float = 0.0, detail: str = "", timestamp: int | None = None) -> None:
        append_csv(self.run_dir / "multileg_events.csv", self.EVENT_FIELDS, {
            "timestamp": int(time.time()) if timestamp is None else int(timestamp), "event": name, "bundle_id": bundle_id,
            "market_id": leg.market_id if leg else "", "side": leg.side if leg else "",
            "shares": shares, "price": price, "queue_ahead": leg.queue_ahead if leg else 0.0,
            "detail": detail,
        })

    def load_state(self) -> None:
        risk = read_csv(self.run_dir / "multileg_risk.csv")
        if risk:
            self.cash = finite(risk[-1].get("cash"), self.cash)
            self.peak = finite(risk[-1].get("peak_equity"), self.cash)
            self.killed = bool(int(finite(risk[-1].get("killed"), 0.0)))
            self.tape_cursor = int(finite(risk[-1].get("tape_cursor"), 0.0))
        for row in read_csv(self.run_dir / "multileg_bundles.csv"):
            try:
                bundle = Bundle(
                    bundle_id=row["bundle_id"], strategy=row["strategy"], event_id=row["event_id"], status=row["status"],
                    created_ts=int(row["created_ts"]), expected_edge=float(row["expected_edge"]), max_notional=float(row["max_notional"]),
                    execution_deadline_ts=int(row["execution_deadline_ts"]), hold_deadline_ts=int(row["hold_deadline_ts"]),
                    ledger_written=bool(int(row.get("ledger_written") or 0)), abort_reason=row.get("abort_reason") or "",
                )
                self.bundles[bundle.bundle_id] = bundle
            except (KeyError, TypeError, ValueError):
                continue
        for row in read_csv(self.run_dir / "multileg_legs.csv"):
            try:
                self.legs.append(Leg(
                    bundle_id=row["bundle_id"], market_id=row["market_id"], risk_event_id=row.get("event_id") or "", side=row["side"], token_id=row["token_id"],
                    weight=float(row["weight"]), target_shares=float(row["target_shares"]), filled_shares=float(row["filled_shares"]),
                    limit_price=float(row["limit_price"]), queue_ahead=float(row["queue_ahead"]), arrival_ms=int(row["arrival_ms"]),
                    arrival_event_ms=int(row.get("arrival_event_ms") or row["arrival_ms"]), cancel_effective_ms=int(row.get("cancel_effective_ms") or 0),
                    entry_cash=float(row["entry_cash"]), entry_fee=float(row["entry_fee"]), exit_cash=float(row["exit_cash"]),
                    exit_fee=float(row["exit_fee"]), slippage_cost=float(row["slippage_cost"]), first_fill_ts=int(row["first_fill_ts"]),
                    last_fill_ts=int(row["last_fill_ts"]), adverse_mark_pnl=float(row["adverse_mark_pnl"]),
                    adverse_recorded=bool(int(row["adverse_recorded"])), order_state=row["order_state"], exited=bool(int(row.get("exited") or 0)),
                ))
            except (KeyError, TypeError, ValueError):
                continue

    def persist(self) -> None:
        atomic_csv(self.run_dir / "multileg_bundles.csv", self.BUNDLE_FIELDS, [
            {**asdict(bundle), "ledger_written": 1 if bundle.ledger_written else 0}
            for bundle in self.bundles.values()
        ])
        rows = []
        for leg in self.legs:
            rows.append({
                "bundle_id": leg.bundle_id, "market_id": leg.market_id, "event_id": leg.risk_event_id, "side": leg.side,
                "token_id": leg.token_id, "weight": leg.weight, "target_shares": leg.target_shares, "filled_shares": leg.filled_shares,
                "limit_price": leg.limit_price, "queue_ahead": leg.queue_ahead, "arrival_ms": leg.arrival_ms, "arrival_event_ms": leg.arrival_event_ms,
                "cancel_effective_ms": leg.cancel_effective_ms, "replace_count": 0, "entry_cash": leg.entry_cash, "entry_fee": leg.entry_fee,
                "exit_cash": leg.exit_cash, "exit_fee": leg.exit_fee, "slippage_cost": leg.slippage_cost,
                "first_fill_ts": leg.first_fill_ts, "last_fill_ts": leg.last_fill_ts, "adverse_mark_pnl": leg.adverse_mark_pnl,
                "adverse_recorded": 1 if leg.adverse_recorded else 0, "order_state": leg.order_state,
                "max_limit_price": leg.limit_price, "exited": 1 if leg.exited else 0,
            })
        atomic_csv(self.run_dir / "multileg_legs.csv", self.LEG_FIELDS, rows)
        atomic_csv(self.run_dir / "multileg_risk.csv", ["cash", "peak_equity", "killed", "tape_cursor"], [{
            "cash": self.cash, "peak_equity": self.peak, "killed": 1 if self.killed else 0, "tape_cursor": self.tape_cursor,
        }])

    def market(self, market_id: str, force: bool = False) -> dict[str, Any] | None:
        now = int(time.time())
        if not force and market_id in self.market_cache and now - self.market_cache_ts.get(market_id, 0) <= 15:
            return self.market_cache[market_id]
        try:
            raw = request_json(f"{self.gamma}/markets/{market_id}")
        except Exception:
            return self.market_cache.get(market_id) if not force else None
        if isinstance(raw, dict):
            self.market_cache[market_id] = raw
            self.market_cache_ts[market_id] = now
            return raw
        return None

    def books(self, tokens: list[str]) -> dict[str, Book]:
        output: dict[str, Book] = {}
        if not tokens:
            return output
        for start in range(0, len(tokens), 80):
            try:
                root = request_json(f"{self.clob}/books", [{"token_id": token} for token in tokens[start:start + 80]])
            except Exception:
                continue
            received = now_ms()
            for raw in root if isinstance(root, list) else []:
                if isinstance(raw, dict):
                    book = parse_book(raw, received)
                    if book:
                        output[book.token] = book
        return output

    def live_legs(self) -> list[Leg]:
        return [leg for leg in self.legs if self.bundles.get(leg.bundle_id) and self.bundles[leg.bundle_id].status not in {"CLOSED", "UNWOUND", "CANCELLED"} and not leg.exited]

    def market_committed(self, market_id: str) -> float:
        return sum(leg.entry_cash + (leg.remaining * leg.limit_price if leg.order_state == "RESTING" else 0.0) for leg in self.live_legs() if leg.market_id == market_id)

    def event_committed(self, risk_event_id: str) -> float:
        return sum(leg.entry_cash + (leg.remaining * leg.limit_price if leg.order_state == "RESTING" else 0.0) for leg in self.live_legs() if leg.risk_event_id == risk_event_id)

    def reserved_cash(self) -> float:
        return sum(leg.remaining * leg.limit_price for leg in self.live_legs() if leg.order_state == "RESTING")

    def gross_entry_cash(self) -> float:
        return sum(leg.entry_cash for leg in self.live_legs())

    def read_intents(self) -> dict[str, list[dict[str, str]]]:
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in read_csv(self.intents):
            bundle_id = str(row.get("bundle_id") or "")
            if bundle_id:
                grouped.setdefault(bundle_id, []).append(row)
        return grouped

    def active_tokens(self) -> set[str]:
        return {leg.token_id for leg in self.live_legs() if leg.order_state == "RESTING"}

    def admit(self, current_equity: float) -> None:
        if self.killed:
            return
        now = int(time.time())
        active_tokens = self.active_tokens()
        for bundle_id, rows in self.read_intents().items():
            if bundle_id in self.bundles or not rows:
                continue
            head = rows[0]
            try:
                edge = float(head["expected_edge"]); max_notional = float(head["max_notional"])
                execution_deadline = int(head["execution_deadline_ts"]); hold_deadline = int(head["hold_deadline_ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if edge <= self.min_edge or max_notional <= 0.0 or execution_deadline <= now or hold_deadline <= now or head.get("mode") != "MAKER":
                continue
            strategy = str(head.get("strategy") or "")
            input_event = str(head.get("event_id") or "")
            prepared: list[tuple[dict[str, str], dict[str, Any], str, str]] = []
            valid = True
            seen_tokens: set[str] = set()
            for row in rows:
                if str(row.get("strategy") or "") != strategy or str(row.get("event_id") or "") != input_event:
                    valid = False; break
                raw = self.market(str(row.get("market_id") or ""), force=True)
                if raw is None or is_closed(raw) or not bool(raw.get("active", True)):
                    valid = False; break
                risk_event = market_event_id(raw)
                if not risk_event:
                    valid = False; break
                if strategy.startswith("GRAPH") and risk_event != input_event:
                    valid = False; break
                token = side_token(raw, str(row.get("side") or ""))
                if not token or token in active_tokens or token in seen_tokens:
                    valid = False; break
                seen_tokens.add(token)
                prepared.append((row, raw, risk_event, token))
            if not valid or len(prepared) < 2:
                continue
            books = self.books([item[3] for item in prepared])
            if any(token not in books for _row, _raw, _risk, token in prepared):
                continue
            capital_per_unit = 0.0
            limits: list[float] = []
            fees = []
            for row, raw, _risk, token in prepared:
                book = books[token]
                limit = finite(row.get("limit_price"), book.bid)
                if not math.isfinite(limit) or limit <= 0.0 or limit >= book.ask - 1e-12:
                    valid = False; break
                details = resolve_fee_details(raw, self.clob, str(raw.get("conditionId") or ""), token)
                if not details.verified:
                    valid = False; break
                weight = max(0.0, finite(row.get("weight"), 0.0))
                if weight <= 0.0:
                    valid = False; break
                capital_per_unit += weight * (limit + fee_per_share(limit, details, taker=False))
                limits.append(limit); fees.append(details)
            if not valid or capital_per_unit <= 1e-12:
                continue
            eq = max(1.0, current_equity)
            room = min(max_notional, float(self.cfg["max_trade_usd"]), max(0.0, float(self.cfg["max_gross_fraction"]) * eq - self.gross_entry_cash() - self.reserved_cash()), max(0.0, self.cash - self.reserved_cash()))
            if room <= 0.0:
                continue
            units = room / capital_per_unit
            for index, (row, _raw, risk_event, token) in enumerate(prepared):
                weight = float(row["weight"]); leg_per_unit = weight * (limits[index] + fee_per_share(limits[index], fees[index], taker=False))
                market_room = float(self.cfg["max_market_fraction"]) * eq - self.market_committed(str(row["market_id"]))
                event_room = float(self.cfg["max_event_fraction"]) * eq - self.event_committed(risk_event)
                if leg_per_unit > 1e-12:
                    units = min(units, max(0.0, market_room) / leg_per_unit, max(0.0, event_room) / leg_per_unit)
                units = min(units, 0.25 * max(1.0, books[token].queue_at(limits[index])) / max(weight, 1e-12))
            if units <= 0.0:
                continue
            if any(units * float(row["weight"]) + 1e-9 < books[token].min_order for row, _raw, _risk, token in prepared):
                continue
            bundle = Bundle(bundle_id, strategy, input_event, "RESTING", int(head["created_ts"]), edge, units * capital_per_unit, execution_deadline, hold_deadline)
            self.bundles[bundle_id] = bundle
            arrival = now_ms() + self.submit_latency_ms
            for index, (row, _raw, risk_event, token) in enumerate(prepared):
                leg = Leg(bundle_id, str(row["market_id"]), risk_event, str(row["side"]), token, float(row["weight"]), units * float(row["weight"]), 0.0, limits[index], books[token].queue_at(limits[index]), arrival, now_ms())
                self.legs.append(leg)
                active_tokens.add(token)
                self.event("POST", bundle_id, leg, price=leg.limit_price, detail=f"risk_event_id={risk_event};queue_ahead={leg.queue_ahead}")

    def read_new_tape(self) -> list[dict[str, Any]]:
        rows = read_csv(self.trade_tape)
        new = rows[self.tape_cursor:]
        self.tape_cursor = len(rows)
        output = []
        for row in new:
            try:
                received_ms = int(row.get("received_ms") or 0)
                if received_ms <= 0:
                    continue
                output.append({
                    "event_ts_ms": int(float(row["timestamp"]) * 1000),
                    "received_ms": received_ms,
                    "asset_id": str(row["asset_id"]), "side": str(row["side"]).upper(),
                    "price": float(row["price"]), "size": float(row["size"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        output.sort(key=lambda row: (row["received_ms"], row["event_ts_ms"]))
        return output

    def causal_cancel_ms(self, leg: Leg) -> int:
        cancel_ms = leg.cancel_effective_ms
        bundle = self.bundles.get(leg.bundle_id)
        if bundle is not None and bundle.execution_deadline_ts > 0:
            deadline_ms = bundle.execution_deadline_ts * 1000
            cancel_ms = deadline_ms if cancel_ms <= 0 else min(cancel_ms, deadline_ms)
        return cancel_ms

    def apply_trades(self, trades: list[dict[str, Any]]) -> None:
        for trade in trades:
            if trade["side"] != "SELL" or trade["size"] <= 0.0 or trade["received_ms"] <= 0 or self.killed:
                continue
            candidates: list[Leg] = []
            for leg in self.live_legs():
                if leg.order_state != "RESTING" or leg.token_id != trade["asset_id"] or trade["price"] > leg.limit_price + 1e-12:
                    continue
                if receive_time_active(trade["received_ms"], leg.arrival_ms, self.causal_cancel_ms(leg)):
                    candidates.append(leg)
            candidates.sort(key=lambda leg: (leg.arrival_ms, leg.bundle_id, leg.market_id))
            remaining_trade = trade["size"]
            for leg in candidates:
                if remaining_trade <= 1e-12:
                    break
                queue_used = min(leg.queue_ahead, remaining_trade)
                leg.queue_ahead -= queue_used; remaining_trade -= queue_used
                fill = min(leg.remaining, remaining_trade)
                if fill <= 1e-12:
                    continue
                raw = self.market(leg.market_id)
                if raw is None:
                    continue
                details = resolve_fee_details(raw, self.clob, str(raw.get("conditionId") or ""), leg.token_id)
                if not details.verified:
                    continue
                fee = fill * fee_per_share(leg.limit_price, details, taker=False)
                cost = fill * leg.limit_price
                if cost + fee > self.cash + 1e-9:
                    self.abort(leg.bundle_id, "capital_during_fill")
                    continue
                self.cash -= cost + fee
                leg.filled_shares += fill; leg.entry_cash += cost; leg.entry_fee += fee
                fill_ts = trade["received_ms"] // 1000
                if leg.first_fill_ts == 0: leg.first_fill_ts = fill_ts
                leg.last_fill_ts = fill_ts
                remaining_trade -= fill
                if leg.remaining <= 1e-9: leg.order_state = "FILLED"
                self.event("PARTIAL_FILL", leg.bundle_id, leg, fill, leg.limit_price, f"received_ms={trade['received_ms']};trade_event_ms={trade['event_ts_ms']}", timestamp=fill_ts)

    def abort(self, bundle_id: str, reason: str) -> None:
        bundle = self.bundles.get(bundle_id)
        if not bundle or bundle.status in {"CLOSED", "UNWOUND", "CANCELLED"}:
            return
        cancel_ms = now_ms()
        if reason == "execution_timeout" and bundle.execution_deadline_ts > 0:
            cancel_ms = min(cancel_ms, bundle.execution_deadline_ts * 1000)
        if bundle.status != "ABORTING":
            bundle.status = "ABORTING"; bundle.abort_reason = reason
            self.event("ABORT", bundle_id, detail=reason)
        for leg in self.legs:
            if leg.bundle_id == bundle_id and leg.order_state == "RESTING":
                leg.cancel_effective_ms = cancel_ms if leg.cancel_effective_ms <= 0 else min(leg.cancel_effective_ms, cancel_ms)
                leg.order_state = "CANCELLED"
                self.event("CANCEL_EFFECTIVE", bundle_id, leg, price=leg.limit_price, detail=f"{reason};cancel_effective_ms={leg.cancel_effective_ms}")

    def bundle_legs(self, bundle_id: str) -> list[Leg]:
        return [leg for leg in self.legs if leg.bundle_id == bundle_id]

    def bundle_complete(self, bundle_id: str) -> bool:
        legs = self.bundle_legs(bundle_id)
        return bool(legs) and all(leg.remaining <= 1e-9 for leg in legs)

    def settle_leg(self, bundle_id: str, leg: Leg, raw: dict[str, Any]) -> bool:
        if leg.exited or not is_closed(raw):
            return leg.exited
        outcome = resolved_yes(raw)
        if outcome is None:
            return False
        wins = (leg.side == "YES" and outcome == 1) or (leg.side == "NO" and outcome == 0)
        payout = leg.filled_shares if wins else 0.0
        leg.exit_cash = payout; leg.exit_fee = 0.0; leg.slippage_cost = 0.0; leg.exited = True; leg.order_state = "DONE"
        self.cash += payout
        self.event("SETTLE", bundle_id, leg, leg.filled_shares, 1.0 if wins else 0.0, "winner" if wins else "loser")
        return True

    def exit_open_leg(self, bundle_id: str, leg: Leg, raw: dict[str, Any], book: Book | None, reason: str) -> bool:
        if leg.exited or leg.filled_shares <= 1e-12:
            leg.exited = True; leg.order_state = "DONE"; return True
        if is_closed(raw):
            return self.settle_leg(bundle_id, leg, raw)
        if book is None:
            return False
        details = resolve_fee_details(raw, self.clob, str(raw.get("conditionId") or ""), leg.token_id)
        if not details.verified:
            return False
        prices = sell_vwap(book, leg.filled_shares, self.slippage_bps)
        if prices is None:
            return False
        raw_avg, slipped = prices
        fee = leg.filled_shares * fee_per_share(slipped, details, taker=True)
        raw_cash = leg.filled_shares * raw_avg; slipped_cash = leg.filled_shares * slipped
        leg.exit_cash = slipped_cash - fee; leg.exit_fee = fee; leg.slippage_cost = raw_cash - slipped_cash
        leg.exited = True; leg.order_state = "DONE"; self.cash += leg.exit_cash
        self.event("EXIT_TAKER", bundle_id, leg, leg.filled_shares, slipped, reason)
        return True

    def write_ledger(self, bundle_id: str) -> None:
        bundle = self.bundles[bundle_id]
        if bundle.ledger_written or bundle.status not in {"CLOSED", "UNWOUND", "CANCELLED"}:
            return
        legs = self.bundle_legs(bundle_id)
        entry = sum(leg.entry_cash + leg.entry_fee for leg in legs)
        exit_cash = sum(leg.exit_cash for leg in legs)
        fees = sum(leg.entry_fee + leg.exit_fee for leg in legs)
        slippage = sum(leg.slippage_cost for leg in legs)
        net = exit_cash - entry
        gross = net + fees + slippage
        fill_fraction = min((leg.fill_fraction for leg in legs), default=0.0)
        adverse = sum(leg.adverse_mark_pnl for leg in legs)
        append_csv(self.run_dir / "bundle_ledger.csv", self.LEDGER_FIELDS, {
            "bundle_id": bundle.bundle_id, "strategy": bundle.strategy, "event_id": bundle.event_id, "created_ts": bundle.created_ts,
            "closed_ts": int(time.time()), "status": bundle.status, "expected_edge": bundle.expected_edge, "max_notional": bundle.max_notional,
            "entry_cash": entry, "gross_pnl": gross, "fees": fees, "slippage": slippage, "net_pnl": net,
            "return_on_capital": net / entry if entry > 1e-12 else 0.0, "fill_fraction": fill_fraction,
            "adverse_mark_pnl": adverse, "abort_reason": bundle.abort_reason,
        })
        bundle.ledger_written = True

    def manage(self, books: dict[str, Book]) -> None:
        now = int(time.time())
        for bundle_id, bundle in list(self.bundles.items()):
            if bundle.status in {"CLOSED", "UNWOUND", "CANCELLED"}:
                self.write_ledger(bundle_id); continue
            legs = self.bundle_legs(bundle_id)
            raws = {leg.market_id: self.market(leg.market_id, force=True) for leg in legs}
            any_closed = any(raw is not None and is_closed(raw) for raw in raws.values())
            if bundle.status == "RESTING" and self.bundle_complete(bundle_id):
                bundle.status = "COMPLETE"
                self.event("BUNDLE_COMPLETE", bundle_id, detail="completion=1.0")
            if bundle.status == "RESTING" and (now >= bundle.execution_deadline_ts or self.killed):
                self.abort(bundle_id, "drawdown_kill" if self.killed else "execution_timeout")
            if bundle.status == "RESTING" and any_closed:
                self.abort(bundle_id, "market_closed_before_complete")
            if bundle.status == "COMPLETE" and any_closed:
                bundle.status = "SETTLING"
                self.event("BUNDLE_SETTLING", bundle_id, detail="preserve_matched_structural_payoff")
            if bundle.status == "COMPLETE" and now >= bundle.hold_deadline_ts:
                done = all(raws[leg.market_id] is not None and self.exit_open_leg(bundle_id, leg, raws[leg.market_id], books.get(leg.token_id), "hold_deadline") for leg in legs)
                if done:
                    bundle.status = "CLOSED"; self.write_ledger(bundle_id)
            elif bundle.status == "SETTLING":
                done = all(raws[leg.market_id] is not None and self.settle_leg(bundle_id, leg, raws[leg.market_id]) for leg in legs)
                if done:
                    bundle.status = "CLOSED"; self.write_ledger(bundle_id)
            elif bundle.status == "ABORTING":
                done = all(raws[leg.market_id] is not None and self.exit_open_leg(bundle_id, leg, raws[leg.market_id], books.get(leg.token_id), "abort_unwind") for leg in legs)
                if done:
                    bundle.status = "UNWOUND"; self.write_ledger(bundle_id)

    def measure_adverse(self, books: dict[str, Book]) -> None:
        now = int(time.time())
        for leg in self.live_legs():
            if leg.adverse_recorded or leg.filled_shares <= 1e-12 or leg.last_fill_ts <= 0 or now - leg.last_fill_ts < self.adverse_horizon_seconds:
                continue
            book = books.get(leg.token_id)
            if book and math.isfinite(book.bid):
                leg.adverse_mark_pnl = leg.filled_shares * (book.bid - leg.entry_avg)
                leg.adverse_recorded = True
                self.event("ADVERSE_MARK", leg.bundle_id, leg, leg.filled_shares, book.bid, f"pnl={leg.adverse_mark_pnl}")

    def equity(self, books: dict[str, Book]) -> float:
        value = self.cash
        for leg in self.live_legs():
            if leg.filled_shares <= 1e-12:
                continue
            raw = self.market(leg.market_id)
            if raw is not None and is_closed(raw):
                outcome = resolved_yes(raw)
                if outcome is not None:
                    wins = (leg.side == "YES" and outcome == 1) or (leg.side == "NO" and outcome == 0)
                    value += leg.filled_shares if wins else 0.0
                    continue
            book = books.get(leg.token_id)
            value += leg.filled_shares * (book.bid if book and math.isfinite(book.bid) else leg.entry_avg)
        return value

    def tick(self) -> None:
        tokens = sorted({leg.token_id for leg in self.live_legs()})
        books = self.books(tokens)
        eq = self.equity(books)
        self.peak = max(self.peak, eq)
        drawdown = max(0.0, 1.0 - eq / self.peak) if self.peak > 0 else 0.0
        self.killed = self.killed or drawdown >= float(self.cfg.get("max_drawdown", 0.15))
        self.admit(eq)
        tokens = sorted({leg.token_id for leg in self.live_legs()})
        books = self.books(tokens)
        self.apply_trades(self.read_new_tape())
        self.manage(books)
        self.measure_adverse(books)
        eq = self.equity(books); self.peak = max(self.peak, eq)
        drawdown = max(0.0, 1.0 - eq / self.peak) if self.peak > 0 else 0.0
        self.killed = self.killed or drawdown >= float(self.cfg.get("max_drawdown", 0.15))
        self.persist()
        append_csv(self.run_dir / "multileg_equity.csv", self.EQUITY_FIELDS, {
            "timestamp": int(time.time()), "cash": self.cash, "equity": eq, "reserved_cash": self.reserved_cash(),
            "gross_entry_cash": self.gross_entry_cash(), "peak_equity": self.peak, "drawdown": drawdown,
            "killed": 1 if self.killed else 0,
            "live_bundles": sum(bundle.status not in {"CLOSED", "UNWOUND", "CANCELLED"} for bundle in self.bundles.values()),
        })
        atomic_json(self.run_dir / "v7_broker_status.json", {
            "timestamp": int(time.time()), "paper_only": True, "authenticated_execution": False,
            "cash": self.cash, "equity": eq, "drawdown": drawdown, "killed": self.killed,
            "reserved_cash": self.reserved_cash(), "gross_entry_cash": self.gross_entry_cash(),
            "bundles": {key: bundle.status for key, bundle in self.bundles.items()},
            "contracts": ["receive_time_causal_forward_fill", "exchange_time_metadata_only_for_fill_causality", "one_live_order_per_token", "shared_trade_capacity", "canonical_market_event_risk", "100_percent_completion", "explicit_abort_unwind", "settling_preserves_complete_structural_payoff"],
        })


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical V7 multi-leg PAPER broker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--submit-latency-ms", type=int, default=100)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--adverse-horizon-seconds", type=int, default=45)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    broker = Broker(args.config, args.run_dir, args.intents, args.trade_tape, args.min_edge, args.submit_latency_ms, args.slippage_bps, args.adverse_horizon_seconds)
    stop = False
    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop; stop = True
    signal.signal(signal.SIGINT, _stop); signal.signal(signal.SIGTERM, _stop)
    while True:
        broker.tick()
        if not args.loop or stop:
            break
        time.sleep(max(0.1, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
