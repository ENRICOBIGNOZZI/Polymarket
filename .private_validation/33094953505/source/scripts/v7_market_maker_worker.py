#!/usr/bin/env python3
"""Canonical PAPER worker for the V7 professional market maker.

This worker is intentionally unauthenticated.  It consumes a slow-path rewarded
market selection, public CLOB books and the canonical public trade tape, creates
post-only PAPER orders, simulates FIFO queue depletion conservatively, keeps
cancel-pending orders fillable through the configured cancel latency, manages
YES/NO inventory including complete-set merges, and emits all economic events
through the single V7 ledger spool.

The REST book loop is an immediately executable PAPER/bootstrap path.  The final
production latency target is the shared C++ WebSocket fast path described in the
policy; no REST call belongs on the real-money quote hot path.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event
from v7_market_common import finite, request_json, resolve_fee_details, fee_per_share
from v7_market_maker_core import (
    BookState,
    ExecutionEstimate,
    InventoryState,
    MakerPolicy,
    RewardContext,
    evaluate_quote,
    fair_value,
    post_only_price,
    reservation_price,
    reward_order_score,
    toxicity_score,
)

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "MICRO_MAKER_PRO"
MARKOUT_HORIZONS = (1, 10, 45, 60, 300)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def exact_sha() -> str:
    value = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise RuntimeError("exact_git_sha_required")
    return value


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()[:32]


def normalize_ts_ms(value: Any) -> int:
    ts = finite(value, 0.0)
    if not math.isfinite(ts) or ts <= 0:
        return 0
    return int(ts * 1000.0) if ts < 10_000_000_000 else int(ts)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, csv.Error):
        return []


@dataclass(frozen=True)
class BookSnapshot:
    token: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    tick: float
    min_order: float
    exchange_ts_ms: int
    receive_ts_ms: int
    snapshot_id: str

    @property
    def bid(self) -> float:
        return self.bids[0][0]

    @property
    def ask(self) -> float:
        return self.asks[0][0]

    @property
    def bid_depth5(self) -> float:
        return sum(q for _, q in self.bids[:5])

    @property
    def ask_depth5(self) -> float:
        return sum(q for _, q in self.asks[:5])

    @property
    def microprice(self) -> float:
        db, da = self.bid_depth5, self.ask_depth5
        if db + da <= 1e-12:
            return 0.5 * (self.bid + self.ask)
        return max(self.bid, min(self.ask, (self.ask * db + self.bid * da) / (db + da)))

    def queue_at(self, price: float, side: str) -> float:
        levels = self.bids if side.upper() == "BUY" else self.asks
        return sum(q for px, q in levels if abs(px - price) <= max(1e-9, self.tick * 0.25))


def parse_book(raw: dict[str, Any], received_ms: int) -> BookSnapshot | None:
    token = str(raw.get("asset_id") or "")
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, out in (("bids", bids), ("asks", asks)):
        rows = raw.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            px = finite(row.get("price"), math.nan)
            qty = max(0.0, finite(row.get("size"), 0.0))
            if math.isfinite(px) and 0.0 < px < 1.0 and qty > 0.0:
                out.append((px, qty))
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    exchange = normalize_ts_ms(raw.get("timestamp"))
    if not token or not bids or not asks or bids[0][0] >= asks[0][0] or exchange <= 0:
        return None
    snapshot = str(raw.get("hash") or "").strip() or stable_id(token, exchange, bids, asks)
    return BookSnapshot(
        token=token,
        bids=bids,
        asks=asks,
        tick=max(1e-6, finite(raw.get("tick_size"), 0.01)),
        min_order=max(1.0, finite(raw.get("min_order_size"), 1.0)),
        exchange_ts_ms=exchange,
        receive_ts_ms=received_ms,
        snapshot_id=snapshot,
    )


def walk(levels: list[tuple[float, float]], shares: float) -> tuple[float, float] | None:
    remaining = max(0.0, shares)
    cash = 0.0
    done = 0.0
    for px, qty in levels:
        take = min(remaining, qty)
        cash += take * px
        done += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if done + 1e-9 < shares or done <= 0.0:
        return None
    return cash / done, cash


class MakerWorker:
    def __init__(self, *, sleeve_config: Path, maker_policy: Path, run_root: Path,
                 selection: Path, reward_scan: Path, model_path: Path, trade_tape: Path):
        self.cfg = json.loads(sleeve_config.read_text(encoding="utf-8"))
        v7 = self.cfg.get("v7") or {}
        if self.cfg.get("paper_only") is not True or v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
            raise RuntimeError("maker_requires_paper_only_authenticated_disabled")
        self.policy_cfg = json.loads(maker_policy.read_text(encoding="utf-8"))
        if self.policy_cfg.get("paper_only") is not True or self.policy_cfg.get("authenticated_execution") is not False or self.policy_cfg.get("real_order_submission") is not False:
            raise RuntimeError("maker_policy_requires_paper_only_authenticated_disabled")
        self.policy = MakerPolicy.from_json(maker_policy)
        self.sha = exact_sha()
        self.root = run_root
        self.dir = run_root / "micro_maker"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.selection_path = selection
        self.reward_scan_path = reward_scan
        self.model_path = model_path
        self.trade_tape = trade_tape
        self.state_path = self.dir / "state.json"
        self.gamma = str(self.cfg.get("gamma_url") or "https://gamma-api.polymarket.com").rstrip("/")
        self.clob = str(self.cfg.get("clob_url") or "https://clob.polymarket.com").rstrip("/")
        starting = float(self.cfg.get("starting_capital", 0.0))
        self.state: dict[str, Any] = {
            "schema": "polymarket_v7_professional_maker_state_v1",
            "model_sha": self.sha,
            "paper_only": True,
            "authenticated_execution": False,
            "cash": starting,
            "starting_capital": starting,
            "peak_equity": starting,
            "tape_cursor_received_ms": 0,
            "sequence": 0,
            "orders": {},
            "fills": {},
            "inventory": {},
            "estimated_liquidity_reward_pnl": 0.0,
            "estimated_maker_rebate_pnl": 0.0,
            "realized_trading_pnl": 0.0,
        }
        try:
            old = json.loads(self.state_path.read_text(encoding="utf-8"))
            if old.get("model_sha") == self.sha and old.get("paper_only") is True:
                self.state.update(old)
        except (OSError, json.JSONDecodeError):
            pass
        self.mid_history: dict[str, list[float]] = {}
        self.previous_depth: dict[str, tuple[float, float]] = {}

    def emit(self, event: LedgerEvent) -> None:
        spool_event(self.root, event)

    def save(self) -> None:
        self.state["updated_ts_ms"] = now_ms()
        atomic_json(self.state_path, self.state)

    def load_selection(self) -> list[dict[str, Any]]:
        try:
            root = json.loads(self.selection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = root.get("markets") if isinstance(root, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def load_reward_scan(self) -> dict[str, dict[str, str]]:
        return {str(row.get("market_id") or ""): row for row in read_csv(self.reward_scan_path) if row.get("market_id")}

    def load_model(self) -> dict[str, Any]:
        try:
            root = json.loads(self.model_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if root.get("model_sha") not in {None, self.sha}:
            return {}
        return root if isinstance(root, dict) else {}

    def estimate(self, model: dict[str, Any], action: str, outcome: str, side: str) -> ExecutionEstimate:
        groups = model.get("groups") if isinstance(model.get("groups"), dict) else {}
        row = groups.get(f"{action.upper()}|{outcome.upper()}|{side.upper()}") or groups.get("GLOBAL") or {}
        return ExecutionEstimate(
            fill_probability=max(0.0, min(1.0, finite(row.get("fill_probability"), self.policy.cold_start_fill_prior))),
            adverse_markout_per_share=max(0.0, finite(row.get("adverse_markout_per_share"), self.policy.cold_start_adverse_markout_per_share)),
            fill_uncertainty=0.25 if bool(row.get("mature")) else 1.0,
            observations=max(0, int(finite(row.get("orders"), 0.0))),
            fills=max(0, int(finite(row.get("filled_orders"), 0.0))),
            event_clusters=max(0, int(finite(row.get("event_clusters"), 0.0))),
        )

    def fetch_books(self, tokens: list[str]) -> dict[str, BookSnapshot]:
        out: dict[str, BookSnapshot] = {}
        unique = list(dict.fromkeys(token for token in tokens if token))
        for start in range(0, len(unique), 80):
            try:
                rows = request_json(f"{self.clob}/books", [{"token_id": token} for token in unique[start:start + 80]])
            except Exception:
                continue
            received = now_ms()
            for raw in rows if isinstance(rows, list) else []:
                if isinstance(raw, dict):
                    book = parse_book(raw, received)
                    if book:
                        out[book.token] = book
        return out

    def book_state(self, book: BookSnapshot) -> BookState:
        previous = self.previous_depth.get(book.token)
        db, da = book.bid_depth5, book.ask_depth5
        if previous is None:
            ofi = 0.0
        else:
            delta_bid, delta_ask = db - previous[0], da - previous[1]
            ofi = (delta_bid - delta_ask) / (abs(delta_bid) + abs(delta_ask) + 1e-9)
        self.previous_depth[book.token] = (db, da)
        mids = self.mid_history.setdefault(book.token, [])
        mids.append(0.5 * (book.bid + book.ask))
        del mids[:-64]
        volatility = 0.0
        if len(mids) >= 3:
            returns = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
            mean = sum(returns) / len(returns)
            volatility = math.sqrt(sum((x - mean) ** 2 for x in returns) / len(returns))
        return BookState(
            token_id=book.token,
            bid=book.bid,
            ask=book.ask,
            bid_depth=db,
            ask_depth=da,
            tick_size=book.tick,
            microprice=book.microprice,
            ofi=max(-1.0, min(1.0, ofi)),
            short_volatility=volatility,
            queue_bid=book.queue_at(book.bid, "BUY"),
            queue_ask=book.queue_at(book.ask, "SELL"),
            exchange_ts_ms=book.exchange_ts_ms,
            receive_ts_ms=book.receive_ts_ms,
            snapshot_id=book.snapshot_id,
        )

    def inventory_row(self, market: dict[str, Any]) -> dict[str, Any]:
        market_id = str(market.get("market_id") or "")
        inventory = self.state["inventory"].setdefault(market_id, {
            "event_id": str(market.get("event_id") or ""),
            "yes_token": str(market.get("yes_token") or ""),
            "no_token": str(market.get("no_token") or ""),
            "yes_shares": 0.0,
            "no_shares": 0.0,
            "yes_cost": 0.0,
            "no_cost": 0.0,
            "cycle": 0,
        })
        return inventory

    def inventory_state(self, market: dict[str, Any]) -> InventoryState:
        row = self.inventory_row(market)
        return InventoryState(
            yes_shares=float(row.get("yes_shares", 0.0)),
            no_shares=float(row.get("no_shares", 0.0)),
            cash=float(self.state.get("cash", 0.0)),
            sleeve_capital=float(self.state.get("starting_capital", 1.0)),
        )

    def reward_context(self, market: dict[str, Any], reward_scan: dict[str, dict[str, str]]) -> RewardContext:
        scan = reward_scan.get(str(market.get("market_id") or ""), {})
        pool = max(0.0, finite(market.get("total_daily_rate"), finite(scan.get("total_daily_rate"), 0.0)))
        book_qmin = max(0.0, finite(scan.get("book_qmin"), 0.0))
        return RewardContext(
            reward_qualified=pool > 0.0,
            max_spread_cents=max(0.0, finite(market.get("rewards_max_spread_cents"), finite(scan.get("rewards_max_spread_cents"), 0.0))),
            min_size=max(0.0, finite(market.get("rewards_min_size"), finite(scan.get("rewards_min_size"), 0.0))),
            pool_daily_usd=pool,
            estimated_competitor_score=book_qmin,
            maker_rebate_fraction=0.0,
            taker_fee_rate=0.0,
            expected_filled_maker_share=0.0,
        )

    def quote_size(self, book: BookState, inventory: InventoryState, exploration: bool, reward: RewardContext) -> float:
        fraction = self.policy.exploration_quote_notional_fraction if exploration else self.policy.max_order_fraction_of_sleeve
        notional = max(0.0, inventory.sleeve_capital) * fraction
        shares = notional / max(book.bid, book.tick_size)
        depth_cap = 0.10 * max(1.0, book.bid_depth)
        shares = min(shares, depth_cap)
        if reward.reward_qualified and reward.min_size > 0.0:
            # Reward qualification is useful only if it fits inside the independent
            # risk/capital budget.  Reward minimum size never grants extra capacity.
            if reward.min_size <= shares + 1e-9:
                shares = max(shares, reward.min_size)
        return max(0.0, shares)

    def best_quote(self, *, outcome: str, book: BookState, fair: float, reservation: float,
                   model: dict[str, Any], reward: RewardContext, inventory: InventoryState,
                   companion_score: float, sell_inventory: float) -> Any | None:
        action_estimates = {action: self.estimate(model, action, outcome, "BUY") for action in ("JOIN", "IMPROVE1", "FADE1", "FADE2")}
        mature = any(est.mature for est in action_estimates.values())
        exploration = not mature and self.policy.exploration_enabled
        size = self.quote_size(book, inventory, exploration, reward)
        if size <= 0.0:
            return None
        candidates = []
        for action, estimate in action_estimates.items():
            if toxicity_score(book, estimate) >= self.policy.toxicity_withdraw_threshold:
                continue
            use_estimate = estimate
            if exploration:
                use_estimate = ExecutionEstimate(
                    fill_probability=max(self.policy.cold_start_fill_prior, min(estimate.fill_probability, 0.25)),
                    adverse_markout_per_share=max(self.policy.cold_start_adverse_markout_per_share, estimate.adverse_markout_per_share),
                    fill_uncertainty=max(0.75, estimate.fill_uncertainty),
                    observations=estimate.observations,
                    fills=estimate.fills,
                    event_clusters=estimate.event_clusters,
                )
            quote = evaluate_quote(
                outcome=outcome, side="BUY", action=action, book=book, fair=fair,
                reservation=reservation, size=size, estimate=use_estimate, reward=reward,
                companion_reward_score=companion_score, inventory=inventory, policy=self.policy,
                rest_seconds=1.0, exploration=exploration,
            )
            if quote:
                candidates.append(quote)
        if sell_inventory > 1e-9:
            for action in ("JOIN", "IMPROVE1", "FADE1"):
                estimate = self.estimate(model, action, outcome, "SELL")
                if toxicity_score(book, estimate) >= self.policy.toxicity_withdraw_threshold:
                    continue
                sell_size = min(size, sell_inventory)
                quote = evaluate_quote(
                    outcome=outcome, side="SELL", action=action, book=book, fair=fair,
                    reservation=reservation, size=sell_size, estimate=estimate, reward=reward,
                    companion_reward_score=companion_score, inventory=inventory, policy=self.policy,
                    rest_seconds=1.0, exploration=not estimate.mature,
                )
                if quote:
                    candidates.append(quote)
        if not candidates:
            return None
        if exploration:
            priority = {"JOIN": 3.0, "FADE1": 2.0, "IMPROVE1": 1.0, "FADE2": 0.5}
            return max(candidates, key=lambda q: (q.reward_score + priority.get(q.action, 0.0), q.expected_total_pnl))
        best = max(candidates, key=lambda q: q.expected_total_pnl)
        if best.expected_total_pnl <= 0.0 or best.total_ev_per_dollar < self.policy.min_exploit_ev_per_dollar:
            return None
        return best

    def desired_quotes(self, market: dict[str, Any], yes_book: BookState, no_book: BookState,
                       model: dict[str, Any], reward: RewardContext) -> list[Any]:
        inv = self.inventory_state(market)
        fair_yes_a = fair_value(yes_book, self.policy)
        fair_yes_b = 1.0 - fair_value(no_book, self.policy)
        fair_yes = max(yes_book.bid, min(yes_book.ask, 0.5 * (fair_yes_a + fair_yes_b)))
        fair_no = 1.0 - fair_yes
        reserve_yes = reservation_price(fair_yes, inv, yes_book, self.policy)
        inverse = InventoryState(inv.no_shares, inv.yes_shares, inv.cash, inv.sleeve_capital)
        reserve_no = reservation_price(fair_no, inverse, no_book, self.policy)

        yes_probe_size = self.quote_size(yes_book, inv, True, reward)
        no_probe_size = self.quote_size(no_book, inv, True, reward)
        yes_join = post_only_price(yes_book, "BUY", "JOIN", self.policy.max_inside_ticks) or yes_book.bid
        no_join = post_only_price(no_book, "BUY", "JOIN", self.policy.max_inside_ticks) or no_book.bid
        yes_companion = reward_order_score(reward.max_spread_cents, abs(no_join - no_book.mid) * 100.0, no_probe_size) if reward.reward_qualified else 0.0
        no_companion = reward_order_score(reward.max_spread_cents, abs(yes_join - yes_book.mid) * 100.0, yes_probe_size) if reward.reward_qualified else 0.0

        yes_quote = self.best_quote(
            outcome="YES", book=yes_book, fair=fair_yes, reservation=reserve_yes, model=model,
            reward=reward, inventory=inv, companion_score=yes_companion, sell_inventory=inv.yes_shares,
        )
        no_quote = self.best_quote(
            outcome="NO", book=no_book, fair=fair_no, reservation=reserve_no, model=model,
            reward=reward, inventory=inv, companion_score=no_companion, sell_inventory=inv.no_shares,
        )
        return [quote for quote in (yes_quote, no_quote) if quote is not None]

    def _active_order_for(self, token: str, side: str) -> dict[str, Any] | None:
        for order in self.state["orders"].values():
            if order.get("token_id") == token and order.get("side") == side and order.get("state") in {"LIVE", "CANCEL_PENDING"}:
                return order
        return None

    def cancel_order(self, order: dict[str, Any], reason: str) -> None:
        if order.get("state") != "LIVE":
            return
        request_ms = now_ms()
        order["state"] = "CANCEL_PENDING"
        order["cancel_requested_ms"] = request_ms
        order["cancel_effective_ms"] = request_ms + max(0, self.policy.cancel_latency_ms)
        order["cancel_reason"] = reason
        self.emit(LedgerEvent(
            event_type="ORDER_STATE", strategy=STRATEGY, model_sha=self.sha,
            order_id=str(order["order_id"]), market_id=str(order["market_id"]),
            event_id=str(order.get("event_id") or ""), token_id=str(order["token_id"]),
            side=str(order["side"]), order_state="CANCEL_PENDING", cancel_reason=reason,
            metadata={"cancel_requested_ms": request_ms, "cancel_effective_ms": order["cancel_effective_ms"]},
        ))

    def expire_cancels(self) -> None:
        current = now_ms()
        for order in self.state["orders"].values():
            if order.get("state") == "CANCEL_PENDING" and current >= int(order.get("cancel_effective_ms") or 0):
                order["state"] = "CANCELLED"
                self.emit(LedgerEvent(
                    event_type="ORDER_STATE", strategy=STRATEGY, model_sha=self.sha,
                    order_id=str(order["order_id"]), market_id=str(order["market_id"]),
                    event_id=str(order.get("event_id") or ""), token_id=str(order["token_id"]),
                    side=str(order["side"]), order_state="CANCELLED",
                    cancel_reason=str(order.get("cancel_reason") or "replace"),
                ))

    def submit_quote(self, market: dict[str, Any], quote: Any, book: BookState) -> None:
        token = book.token_id
        active = self._active_order_for(token, quote.side)
        if active:
            same = abs(float(active.get("price", 0.0)) - quote.price) <= book.tick_size * 0.25 and str(active.get("action")) == quote.action
            age = now_ms() - int(active.get("arrival_ms") or 0)
            if same and age < 5000:
                return
            if age >= 100:
                self.cancel_order(active, "quote_reprice")
            return

        sequence = int(self.state.get("sequence", 0)) + 1
        self.state["sequence"] = sequence
        decision = now_ms()
        order_id = f"paper-maker:{self.sha[:8]}:{sequence}:{token}:{quote.side}"
        market_id = str(market.get("market_id") or "")
        event_id = str(market.get("event_id") or "")
        outcome = quote.outcome
        queue = book.queue_bid if quote.side == "BUY" and abs(quote.price - book.bid) <= book.tick_size * .25 else (
            book.queue_ask if quote.side == "SELL" and abs(quote.price - book.ask) <= book.tick_size * .25 else 0.0
        )
        common = dict(
            strategy=STRATEGY, model_sha=self.sha, market_id=market_id, event_id=event_id,
            token_id=token, decision_ts_ms=decision, exchange_ts_ms=book.exchange_ts_ms,
            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
            side=quote.side, bid=book.bid, ask=book.ask, bid_depth=book.bid_depth,
            ask_depth=book.ask_depth, queue_ahead=queue, limit_price=quote.price,
            predicted_fill_probability=quote.fill_probability, expected_ev=quote.expected_total_pnl,
            intended_action=quote.action, intended_size=quote.size,
            metadata={
                "outcome": outcome,
                "exploration": quote.exploration,
                "promotion_credit": quote.promotion_credit,
                "fair_value": quote.fair_value,
                "reservation_price": quote.reservation_price,
                "trading_edge_per_share": quote.trading_edge_per_share,
                "expected_trading_pnl": quote.expected_trading_pnl,
                "expected_maker_rebate_pnl": quote.expected_rebate_pnl,
                "expected_liquidity_reward_pnl": quote.expected_liquidity_reward_pnl,
                "subsidy_dependent": quote.subsidy_dependent,
                "reward_score": quote.reward_score,
                "reward_qualified": quote.reward_qualified,
                "ofi": book.ofi,
                "depth_imbalance": book.imbalance,
                "short_volatility": book.short_volatility,
                "post_only": True,
                "queue_never_grants_size": True,
            },
        )
        candidate_id = stable_id("maker-candidate", order_id)
        self.emit(LedgerEvent(event_type="CANDIDATE", candidate_id=candidate_id, **common))
        self.emit(LedgerEvent(event_type="ORDER_SUBMITTED", candidate_id=candidate_id, order_id=order_id, order_state="LIVE", **common))
        self.state["orders"][order_id] = {
            "order_id": order_id, "candidate_id": candidate_id, "market_id": market_id,
            "event_id": event_id, "token_id": token, "outcome": outcome, "side": quote.side,
            "action": quote.action, "price": quote.price, "size": quote.size,
            "remaining": quote.size, "queue_ahead": queue, "arrival_ms": decision,
            "state": "LIVE", "exploration": quote.exploration,
            "promotion_credit": quote.promotion_credit,
        }

    def process_trades(self) -> None:
        rows = read_csv(self.trade_tape)
        cursor = int(self.state.get("tape_cursor_received_ms", 0))
        new_rows = []
        max_received = cursor
        for row in rows:
            received = int(finite(row.get("received_ms"), 0.0))
            if received <= cursor:
                continue
            max_received = max(max_received, received)
            new_rows.append((received, row))
        new_rows.sort(key=lambda x: (x[0], x[1].get("transaction_hash", "")))
        for received, trade in new_rows:
            token = str(trade.get("asset_id") or "")
            side = str(trade.get("side") or "").upper()
            price = finite(trade.get("price"), math.nan)
            available = max(0.0, finite(trade.get("size"), 0.0))
            if not token or side not in {"BUY", "SELL"} or not math.isfinite(price) or available <= 0:
                continue
            orders = [order for order in self.state["orders"].values()
                      if order.get("token_id") == token and order.get("state") in {"LIVE", "CANCEL_PENDING"}
                      and received >= int(order.get("arrival_ms") or 0)
                      and (order.get("state") != "CANCEL_PENDING" or received < int(order.get("cancel_effective_ms") or 0))]
            orders.sort(key=lambda order: int(order.get("arrival_ms") or 0))
            for order in orders:
                if available <= 1e-9:
                    break
                own_side = str(order.get("side") or "").upper()
                limit = float(order.get("price") or 0.0)
                compatible = (own_side == "BUY" and side == "SELL" and price <= limit + 1e-12) or (
                    own_side == "SELL" and side == "BUY" and price >= limit - 1e-12)
                if not compatible:
                    continue
                queue = max(0.0, float(order.get("queue_ahead") or 0.0))
                queue_used = min(queue, available)
                queue -= queue_used
                available -= queue_used
                order["queue_ahead"] = queue
                fill = min(max(0.0, float(order.get("remaining") or 0.0)), available)
                if fill <= 1e-9:
                    continue
                available -= fill
                order["remaining"] = max(0.0, float(order["remaining"]) - fill)
                self.apply_fill(order, fill, limit, received, trade)
                if order["remaining"] <= 1e-9:
                    order["state"] = "FILLED"
                    self.emit(LedgerEvent(
                        event_type="ORDER_STATE", strategy=STRATEGY, model_sha=self.sha,
                        order_id=str(order["order_id"]), market_id=str(order["market_id"]),
                        event_id=str(order.get("event_id") or ""), token_id=str(order["token_id"]),
                        side=str(order["side"]), order_state="FILLED",
                    ))
        self.state["tape_cursor_received_ms"] = max_received

    def apply_fill(self, order: dict[str, Any], shares: float, price: float, received_ms: int,
                   trade: dict[str, str]) -> None:
        fill_id = stable_id("maker-fill", order["order_id"], trade.get("transaction_hash"), received_ms, shares)
        if fill_id in self.state["fills"]:
            return
        market_id = str(order["market_id"])
        inv = self.state["inventory"].setdefault(market_id, {
            "event_id": str(order.get("event_id") or ""), "yes_token": "", "no_token": "",
            "yes_shares": 0.0, "no_shares": 0.0, "yes_cost": 0.0, "no_cost": 0.0, "cycle": 0,
        })
        outcome = str(order.get("outcome") or "").upper()
        side = str(order.get("side") or "").upper()
        key_shares = "yes_shares" if outcome == "YES" else "no_shares"
        key_cost = "yes_cost" if outcome == "YES" else "no_cost"
        realized_sale_pnl = 0.0
        if side == "BUY":
            self.state["cash"] = float(self.state.get("cash", 0.0)) - shares * price
            inv[key_shares] = float(inv.get(key_shares, 0.0)) + shares
            inv[key_cost] = float(inv.get(key_cost, 0.0)) + shares * price
        else:
            current_shares = max(0.0, float(inv.get(key_shares, 0.0)))
            avg_cost = float(inv.get(key_cost, 0.0)) / current_shares if current_shares > 1e-12 else 0.0
            sold = min(shares, current_shares)
            self.state["cash"] = float(self.state.get("cash", 0.0)) + sold * price
            inv[key_shares] = current_shares - sold
            inv[key_cost] = max(0.0, float(inv.get(key_cost, 0.0)) - sold * avg_cost)
            realized_sale_pnl = sold * (price - avg_cost)
            self.state["realized_trading_pnl"] = float(self.state.get("realized_trading_pnl", 0.0)) + realized_sale_pnl

        exchange_ms = normalize_ts_ms(trade.get("timestamp"))
        self.emit(LedgerEvent(
            event_type="FILL", strategy=STRATEGY, model_sha=self.sha,
            order_id=str(order["order_id"]), fill_id=fill_id, market_id=market_id,
            event_id=str(order.get("event_id") or ""), token_id=str(order["token_id"]),
            side=side, fill_price=price, filled_size=shares, exchange_ts_ms=max(1, exchange_ms),
            receive_ts_ms=received_ms, fee=0.0, fee_rate=0.0,
            fee_source="polymarket:maker_fee_zero", complete=order.get("remaining", 0.0) <= 1e-9,
            metadata={
                "outcome": outcome, "paper_fifo_queue": True,
                "public_trade_tx": str(trade.get("transaction_hash") or ""),
                "public_trade_side": str(trade.get("side") or ""),
                "exploration": bool(order.get("exploration")),
                "promotion_credit": bool(order.get("promotion_credit")),
                "realized_sale_pnl": realized_sale_pnl,
            },
        ))
        self.state["fills"][fill_id] = {
            "fill_id": fill_id, "order_id": order["order_id"], "market_id": market_id,
            "event_id": str(order.get("event_id") or ""), "token_id": order["token_id"],
            "outcome": outcome, "side": side, "price": price, "shares": shares,
            "received_ms": received_ms, "markouts_emitted": [],
        }
        self.merge_complete_sets(market_id)

    def merge_complete_sets(self, market_id: str) -> None:
        inv = self.state["inventory"].get(market_id)
        if not isinstance(inv, dict):
            return
        yes = max(0.0, float(inv.get("yes_shares", 0.0)))
        no = max(0.0, float(inv.get("no_shares", 0.0)))
        merge = min(yes, no)
        if merge <= 1e-9:
            return
        avg_yes = float(inv.get("yes_cost", 0.0)) / yes if yes > 1e-12 else 0.0
        avg_no = float(inv.get("no_cost", 0.0)) / no if no > 1e-12 else 0.0
        pnl = merge * (1.0 - avg_yes - avg_no)
        inv["yes_shares"] = yes - merge
        inv["no_shares"] = no - merge
        inv["yes_cost"] = max(0.0, float(inv.get("yes_cost", 0.0)) - merge * avg_yes)
        inv["no_cost"] = max(0.0, float(inv.get("no_cost", 0.0)) - merge * avg_no)
        inv["cycle"] = int(inv.get("cycle", 0)) + 1
        self.state["cash"] = float(self.state.get("cash", 0.0)) + merge
        self.state["realized_trading_pnl"] = float(self.state.get("realized_trading_pnl", 0.0)) + pnl
        self.emit(LedgerEvent(
            event_type="FINAL", strategy=STRATEGY, model_sha=self.sha,
            position_id=f"maker:{market_id}:merge:{inv['cycle']}", market_id=market_id,
            event_id=str(inv.get("event_id") or ""), final_pnl=pnl,
            realized_cashflow=merge, metadata={
                "terminal_type": "COMPLETE_SET_MERGE", "merged_shares": merge,
                "avg_yes_cost": avg_yes, "avg_no_cost": avg_no,
                "trading_pnl_only": True,
            },
        ))

    def emit_markouts(self, books: dict[str, BookSnapshot]) -> None:
        current = now_ms()
        for fill in self.state["fills"].values():
            emitted = set(int(x) for x in fill.get("markouts_emitted", []))
            book = books.get(str(fill.get("token_id") or ""))
            if book is None:
                continue
            for horizon in MARKOUT_HORIZONS:
                if horizon in emitted or current < int(fill["received_ms"]) + horizon * 1000:
                    continue
                shares = float(fill["shares"])
                if str(fill["side"]).upper() == "BUY":
                    walked = walk(book.bids, shares)
                    if walked is None:
                        continue
                    _, liquidation = walked
                    pnl_per_share = (liquidation - shares * float(fill["price"])) / shares
                    executable_value = liquidation
                else:
                    walked = walk(book.asks, shares)
                    if walked is None:
                        continue
                    _, buyback = walked
                    pnl_per_share = (shares * float(fill["price"]) - buyback) / shares
                    executable_value = shares * float(fill["price"]) - buyback
                self.emit(LedgerEvent(
                    event_type="MARKOUT", strategy=STRATEGY, model_sha=self.sha,
                    order_id=str(fill["order_id"]), fill_id=str(fill["fill_id"]),
                    market_id=str(fill["market_id"]), event_id=str(fill.get("event_id") or ""),
                    token_id=str(fill["token_id"]), side=str(fill["side"]),
                    exchange_ts_ms=book.exchange_ts_ms, receive_ts_ms=book.receive_ts_ms,
                    book_snapshot_id=book.snapshot_id,
                    executable_liquidation_value=executable_value,
                    markouts={f"{horizon}s": pnl_per_share},
                    metadata={"pnl_per_share": pnl_per_share, "full_visible_depth": True},
                ))
                emitted.add(horizon)
            fill["markouts_emitted"] = sorted(emitted)

    def reconcile_quotes(self, selections: list[dict[str, Any]], books: dict[str, BookSnapshot],
                         model: dict[str, Any], reward_scan: dict[str, dict[str, str]]) -> None:
        for market in selections:
            market_id = str(market.get("market_id") or "")
            yes_token = str(market.get("yes_token") or "")
            no_token = str(market.get("no_token") or "")
            yes_raw, no_raw = books.get(yes_token), books.get(no_token)
            if not market_id or yes_raw is None or no_raw is None:
                continue
            yes_book, no_book = self.book_state(yes_raw), self.book_state(no_raw)
            reward = self.reward_context(market, reward_scan)
            desired = self.desired_quotes(market, yes_book, no_book, model, reward)
            desired_keys = {(q.outcome, q.side) for q in desired}
            for order in list(self.state["orders"].values()):
                if order.get("market_id") != market_id or order.get("state") != "LIVE":
                    continue
                if (str(order.get("outcome")), str(order.get("side"))) not in desired_keys:
                    self.cancel_order(order, "maker_withdraw")
            for quote in desired:
                book = yes_book if quote.outcome == "YES" else no_book
                self.submit_quote(market, quote, book)

    def cycle(self) -> None:
        self.expire_cancels()
        self.process_trades()
        selections = self.load_selection()
        max_active = int(((self.policy_cfg.get("market_selection") or {}).get("max_active_markets", 40)))
        selections = selections[:max(1, max_active)]
        tokens = [str(row.get(key) or "") for row in selections for key in ("yes_token", "no_token")]
        books = self.fetch_books(tokens)
        self.emit_markouts(books)
        self.reconcile_quotes(selections, books, self.load_model(), self.load_reward_scan())
        self.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="maker sleeve config from v7_capital_allocator")
    parser.add_argument("--maker-policy", type=Path, default=Path("config/v7_professional_market_maker.json"))
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--reward-scan", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-ms", type=int, default=1000)
    args = parser.parse_args()
    worker = MakerWorker(
        sleeve_config=args.config, maker_policy=args.maker_policy, run_root=args.run_root,
        selection=args.selection, reward_scan=args.reward_scan, model_path=args.model,
        trade_tape=args.trade_tape,
    )
    while True:
        started = now_ms()
        try:
            worker.cycle()
        except Exception as exc:
            status = {
                "schema": "polymarket_v7_professional_maker_error_v1",
                "paper_only": True,
                "authenticated_execution": False,
                "model_sha": worker.sha,
                "timestamp_ms": now_ms(),
                "error": f"{type(exc).__name__}:{exc}",
            }
            atomic_json(worker.dir / "error_status.json", status)
        if not args.loop:
            return 0
        elapsed = now_ms() - started
        time.sleep(max(0.01, (max(10, args.interval_ms) - elapsed) / 1000.0))


if __name__ == "__main__":
    raise SystemExit(main())
