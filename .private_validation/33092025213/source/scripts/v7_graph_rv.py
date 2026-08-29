#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event
from v7_market_common import fee_per_share, finite, parse_array, request_json, resolve_fee_details

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "GRAPH_RV"


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


def receive_time_active(received_ms: int, arrival_ms: int, cancel_effective_ms: int = 0) -> bool:
    if received_ms <= 0 or received_ms < arrival_ms:
        return False
    return cancel_effective_ms <= 0 or received_ms < cancel_effective_ms


def queue_decoupled_units(*, risk_units: float, weight: float, unwind_depth: float, unwind_fraction: float) -> float:
    if risk_units <= 0 or weight <= 0 or unwind_depth <= 0 or unwind_fraction <= 0:
        return 0.0
    return min(risk_units, unwind_fraction * unwind_depth / weight)


def allocate_public_trade(queue_ahead: float, own_remaining: float, public_size: float) -> tuple[float, float, float]:
    queue = max(0.0, float(queue_ahead))
    remaining = max(0.0, float(public_size))
    queue_used = min(queue, remaining)
    queue -= queue_used
    remaining -= queue_used
    fill = min(max(0.0, float(own_remaining)), remaining)
    remaining -= fill
    return queue, fill, remaining


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


def market_event_id(raw: dict[str, Any]) -> str:
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        value = str(events[0].get("id") or "")
        if value:
            return value
    return str(raw.get("eventId") or raw.get("event_id") or "")


def side_token(raw: dict[str, Any], side: str) -> str:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().upper() for x in parse_array(raw.get("outcomes"))]
    for i, name in enumerate(outcomes[:len(ids)]):
        if name == side.upper():
            return ids[i]
    if len(ids) >= 2:
        return ids[0] if side.upper() == "YES" else ids[1]
    return ""


def resolved_yes(raw: dict[str, Any]) -> int | None:
    outcomes = [str(x).strip().upper() for x in parse_array(raw.get("outcomes"))]
    prices = [finite(x) for x in parse_array(raw.get("outcomePrices"))]
    for name, price in zip(outcomes, prices):
        if math.isfinite(price) and abs(price - 1.0) <= 1e-9:
            if name == "YES": return 1
            if name == "NO": return 0
    return None


@dataclass(frozen=True)
class Book:
    token: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    tick: float
    min_order: float
    exchange_ts_ms: int
    receive_ts_ms: int
    snapshot_id: str

    @property
    def bid(self) -> float: return self.bids[0][0]
    @property
    def ask(self) -> float: return self.asks[0][0]
    @property
    def bid_depth(self) -> float: return sum(q for _, q in self.bids)
    @property
    def ask_depth(self) -> float: return sum(q for _, q in self.asks)
    def queue_at(self, price: float) -> float:
        return sum(q for px, q in self.bids if abs(px - price) <= max(1e-9, self.tick * 0.25))


def parse_book(raw: dict[str, Any], received_ms: int) -> Book | None:
    token = str(raw.get("asset_id") or "")
    bids, asks = [], []
    for key, out in (("bids", bids), ("asks", asks)):
        for row in raw.get(key, []):
            if not isinstance(row, dict): continue
            px, qty = finite(row.get("price")), max(0.0, finite(row.get("size"), 0.0))
            if math.isfinite(px) and 0 < px < 1 and qty > 0: out.append((px, qty))
    bids.sort(reverse=True); asks.sort()
    exchange = normalize_ts_ms(raw.get("timestamp"))
    if not token or not bids or not asks or exchange <= 0 or exchange > received_ms:
        return None
    snapshot = str(raw.get("hash") or "").strip() or stable_id(token, exchange, bids, asks)
    return Book(token, bids, asks, max(1e-6, finite(raw.get("tick_size"), .01)), max(1.0, finite(raw.get("min_order_size"), 1.0)), exchange, received_ms, snapshot)


def walk(levels: list[tuple[float, float]], shares: float, *, buy: bool, slippage_bps: float) -> tuple[float, float] | None:
    remaining, cash, done = max(0.0, shares), 0.0, 0.0
    for px, qty in levels:
        take = min(remaining, qty); cash += take * px; done += take; remaining -= take
        if remaining <= 1e-9: break
    if done + 1e-9 < shares or done <= 0: return None
    raw = cash / done; slip = max(0.0, slippage_bps) / 10000.0
    effective = raw * (1 + slip if buy else 1 - slip)
    return raw, min(.999999, max(.000001, effective))


def load_joint_model(path: Path | None) -> dict[str, Any]:
    try:
        root = json.loads(path.read_text()) if path and path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return root if isinstance(root, dict) else {}


def direct_joint_state(model: dict[str, Any], leg_count: int, style: tuple[str, ...]) -> tuple[float | None, float]:
    if all(x == "TAKER" for x in style): return 1.0, 0.0
    raw = (((model.get("signatures") or {}).get(str(leg_count)) or {}).get("/".join(style)))
    if not isinstance(raw, dict): return None, 0.0
    p = finite(raw.get("p_complete")); partial = max(0.0, finite(raw.get("expected_partial_unwind_per_unit"), 0.0))
    return (p if math.isfinite(p) and 0 <= p <= 1 else None), partial


def choose_style(prepared: list[dict[str, Any]], model: dict[str, Any]) -> tuple[tuple[str, ...], float | None, float | None]:
    styles = list(itertools.product(("MAKER", "TAKER"), repeat=len(prepared))) if len(prepared) <= 4 else [("MAKER",) * len(prepared), ("TAKER",) * len(prepared)]
    best = None
    for style in styles:
        cost = 0.0
        for item, action in zip(prepared, style):
            book, fee = item["book"], item["fee"]
            px = book.bid if action == "MAKER" else book.ask
            cost += item["weight"] * (px + fee_per_share(px, fee, taker=action == "TAKER"))
        alpha = 1.0 - cost
        p, partial = direct_joint_state(model, len(prepared), style)
        if p is None: continue
        ev = p * alpha - partial
        if best is None or ev > best[0]: best = (ev, style, p)
    if best and best[0] > 0: return best[1], best[2], best[0]
    return ("MAKER",) * len(prepared), None, None


class Broker:
    def __init__(self, config: Path, run_root: Path, intents: Path, tape: Path, joint_model: Path | None):
        self.cfg = json.loads(config.read_text())
        v7 = self.cfg.get("v7") or {}
        if self.cfg.get("paper_only") is not True or v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
            raise RuntimeError("paper_only_authenticated_disabled_required")
        self.gamma, self.clob = str(self.cfg["gamma_url"]).rstrip("/"), str(self.cfg["clob_url"]).rstrip("/")
        self.root, self.dir, self.intents, self.tape = run_root, run_root / "graph_rv", intents, tape
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sha, self.model = exact_sha(), load_joint_model(joint_model)
        self.state_path = self.dir / "state.json"
        self.state = {"model_sha": self.sha, "cash": float(self.cfg.get("starting_capital", 10000)), "peak": float(self.cfg.get("starting_capital", 10000)), "killed": False, "tape_cursor": 0, "bundles": {}}
        try:
            old = json.loads(self.state_path.read_text())
            if old.get("model_sha") == self.sha: self.state.update(old)
        except (OSError, json.JSONDecodeError): pass

    def emit(self, event: LedgerEvent) -> None: spool_event(self.root, event)
    def market(self, market_id: str) -> dict[str, Any] | None:
        try:
            raw = request_json(f"{self.gamma}/markets/{market_id}")
            return raw if isinstance(raw, dict) else None
        except Exception: return None
    def books(self, tokens: list[str]) -> dict[str, Book]:
        out = {}
        for start in range(0, len(tokens), 80):
            try: rows = request_json(f"{self.clob}/books", [{"token_id": t} for t in tokens[start:start+80]])
            except Exception: continue
            received = now_ms()
            for raw in rows if isinstance(rows, list) else []:
                if isinstance(raw, dict):
                    book = parse_book(raw, received)
                    if book: out[book.token] = book
        return out

    def admit(self) -> None:
        if self.state["killed"]: return
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in read_csv(self.intents):
            if row.get("bundle_id"): grouped.setdefault(row["bundle_id"], []).append(row)
        for bundle_id, rows in grouped.items():
            if bundle_id in self.state["bundles"] or len(rows) < 2: continue
            try:
                edge, max_notional = float(rows[0]["expected_edge"]), float(rows[0]["max_notional"])
                deadline, hold = int(rows[0]["execution_deadline_ts"]), int(rows[0]["hold_deadline_ts"])
            except (KeyError, ValueError): continue
            if edge <= float(self.cfg.get("min_net_edge", .00005)) or deadline <= time.time(): continue
            prepared = []
            for row in rows:
                raw = self.market(str(row.get("market_id") or ""))
                if not raw or raw.get("closed") or not raw.get("active", True): prepared = []; break
                event = market_event_id(raw); token = side_token(raw, str(row.get("side") or ""))
                if not event or not token: prepared = []; break
                book = self.books([token]).get(token)
                fee = resolve_fee_details(raw, self.clob, str(raw.get("conditionId") or ""), token)
                weight = max(0.0, finite(row.get("weight"), 0.0))
                if not book or not fee.verified or weight <= 0: prepared = []; break
                prepared.append({"row": row, "raw": raw, "event": event, "token": token, "book": book, "fee": fee, "weight": weight})
            if len(prepared) != len(rows): continue
            style, p_complete, expected_ev = choose_style(prepared, self.model)
            eq = max(1.0, float(self.state["cash"])); room = min(max_notional, eq * float(self.cfg.get("max_trade_fraction", 1.0)))
            cost_per_unit = sum(x["weight"] * (x["book"].bid if a == "MAKER" else x["book"].ask) for x, a in zip(prepared, style))
            if room <= 0 or cost_per_unit <= 0: continue
            units = room / cost_per_unit
            unwind_fraction = float((self.cfg.get("v7") or {}).get("graph_unwind_depth_fraction", .25))
            for x, action in zip(prepared, style):
                units = min(units, queue_decoupled_units(risk_units=units, weight=x["weight"], unwind_depth=x["book"].bid_depth, unwind_fraction=unwind_fraction))
                if action == "TAKER": units = min(units, x["book"].ask_depth / x["weight"])
            if units <= 0 or any(units * x["weight"] + 1e-9 < x["book"].min_order for x in prepared): continue
            decision = now_ms(); legs = {}; target = {}
            for i, (x, action) in enumerate(zip(prepared, style)):
                book = x["book"]; leg_id = f"leg-{i}"; shares = units * x["weight"]
                limit = book.bid if action == "MAKER" else book.ask; queue = book.queue_at(limit) if action == "MAKER" else 0.0
                order_id = f"paper:{bundle_id}:{leg_id}"; target[leg_id] = shares
                common = dict(strategy=STRATEGY, model_sha=self.sha, bundle_id=bundle_id, order_id=order_id, leg_id=leg_id, market_id=str(x["row"]["market_id"]), event_id=x["event"], token_id=x["token"], decision_ts_ms=decision, exchange_ts_ms=book.exchange_ts_ms, receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id, side=str(x["row"]["side"]).upper(), bid=book.bid, ask=book.ask, bid_depth=book.bid_depth, ask_depth=book.ask_depth, queue_ahead=queue, limit_price=limit, predicted_alpha=edge, predicted_fill_probability=p_complete, expected_ev=expected_ev, intended_action=action, intended_size=shares, metadata={"target_quantities": target, "entry_style": "/".join(style), "queue_never_grants_size": True, "unwind_depth_bounds_size": True})
                self.emit(LedgerEvent(event_type="CANDIDATE", candidate_id=f"{bundle_id}:{leg_id}", **common))
                self.emit(LedgerEvent(event_type="ORDER_SUBMITTED", order_state="RESTING" if action == "MAKER" else "CROSS", **common))
                legs[leg_id] = {"leg_id": leg_id, "order_id": order_id, "market_id": common["market_id"], "event_id": x["event"], "token_id": x["token"], "side": common["side"], "weight": x["weight"], "target": shares, "filled": 0.0, "entry_action": action, "limit": limit, "queue": queue, "arrival_ms": decision + int((self.cfg.get("v7") or {}).get("graph_submit_latency_ms", 100)), "cancel_ms": deadline * 1000, "fills": [], "book": {"snapshot_id": book.snapshot_id}} 
            bundle = {"bundle_id": bundle_id, "event_id": str(rows[0]["event_id"]), "status": "RESTING", "expected_edge": edge, "execution_deadline_ts": deadline, "hold_deadline_ts": hold, "legs": legs, "final": False}
            self.state["bundles"][bundle_id] = bundle
            # Sequential taker execution is revalidated leg-by-leg.
            for leg in legs.values():
                if leg["entry_action"] == "TAKER" and not self.fill_taker(bundle, leg):
                    bundle["status"] = "ABORTING"; bundle["abort_reason"] = "taker_revalidation_failed"; break
            self.refresh(bundle)

    def record_fill(self, bundle: dict[str, Any], leg: dict[str, Any], shares: float, price: float, exchange: int, receive: int, *, taker: bool, snapshot: str) -> bool:
        raw = self.market(leg["market_id"])
        if not raw: return False
        fee = resolve_fee_details(raw, self.clob, str(raw.get("conditionId") or ""), leg["token_id"])
        if not fee.verified: return False
        amount = shares * fee_per_share(price, fee, taker=taker); cash = shares * price + amount
        if cash > float(self.state["cash"]) + 1e-9: return False
        self.state["cash"] -= cash; leg["filled"] += shares
        fid = stable_id(self.sha, bundle["bundle_id"], leg["leg_id"], len(leg["fills"]), exchange, receive)
        leg["fills"].append({"fill_id": fid, "shares": shares, "price": price, "fee": amount, "fee_rate": fee.rate, "fee_source": fee.source, "exchange": exchange, "receive": receive, "markouts": []})
        self.emit(LedgerEvent(event_type="FILL", strategy=STRATEGY, model_sha=self.sha, bundle_id=bundle["bundle_id"], order_id=leg["order_id"], fill_id=fid, leg_id=leg["leg_id"], market_id=leg["market_id"], event_id=leg["event_id"], token_id=leg["token_id"], exchange_ts_ms=exchange, receive_ts_ms=receive, side=leg["side"], fill_price=price, filled_size=shares, complete=leg["filled"] + 1e-9 >= leg["target"], fee=amount, fee_rate=fee.rate, fee_source=fee.source, metadata={"bundle_state": bundle["status"]}))
        return True

    def fill_taker(self, bundle: dict[str, Any], leg: dict[str, Any]) -> bool:
        book = self.books([leg["token_id"]]).get(leg["token_id"])
        if not book: return False
        remaining = leg["target"] - leg["filled"]; walked = walk(book.asks, remaining, buy=True, slippage_bps=float(self.cfg.get("slippage_bps", 5.0)))
        return bool(walked and self.record_fill(bundle, leg, remaining, walked[1], book.exchange_ts_ms, book.receive_ts_ms, taker=True, snapshot=book.snapshot_id))

    def read_new_trades(self) -> list[dict[str, Any]]:
        rows = read_csv(self.tape); start = int(self.state.get("tape_cursor", 0)); self.state["tape_cursor"] = len(rows); out = []
        for row in rows[start:]:
            try:
                receive = int(row.get("received_ms") or 0); exchange = int(float(row["timestamp"]) * 1000)
                if receive <= 0 or exchange <= 0 or exchange > receive: continue
                out.append({"receive": receive, "exchange": exchange, "token": str(row["asset_id"]), "side": str(row["side"]).upper(), "price": float(row["price"]), "size": float(row["size"])})
            except (KeyError, ValueError): continue
        out.sort(key=lambda x: (x["receive"], x["exchange"])); return out

    def apply_trades(self) -> None:
        for trade in self.read_new_trades():
            if trade["side"] != "SELL" or trade["size"] <= 0: continue
            candidates = []
            for bundle in self.state["bundles"].values():
                if bundle["status"] not in {"RESTING", "PARTIAL"}: continue
                for leg in bundle["legs"].values():
                    if leg["entry_action"] == "MAKER" and leg["token_id"] == trade["token"] and trade["price"] <= leg["limit"] + 1e-12 and receive_time_active(trade["receive"], leg["arrival_ms"], leg["cancel_ms"]): candidates.append((bundle, leg))
            candidates.sort(key=lambda x: (x[1]["arrival_ms"], x[0]["bundle_id"], x[1]["leg_id"])); remaining = trade["size"]
            for bundle, leg in candidates:
                if remaining <= 1e-12: break
                leg["queue"], fill, remaining_after = allocate_public_trade(leg["queue"], leg["target"] - leg["filled"], remaining)
                if fill > 0 and self.record_fill(bundle, leg, fill, leg["limit"], trade["exchange"], trade["receive"], taker=False, snapshot=leg["book"]["snapshot_id"]): remaining = remaining_after
                elif fill <= 0: remaining = remaining_after
                self.refresh(bundle)

    def refresh(self, bundle: dict[str, Any]) -> None:
        full = all(x["filled"] + 1e-9 >= x["target"] for x in bundle["legs"].values()); any_fill = any(x["filled"] > 1e-9 for x in bundle["legs"].values())
        if full: bundle["status"] = "COMPLETE"
        elif any_fill and bundle["status"] != "ABORTING": bundle["status"] = "PARTIAL"

    def unwind(self, bundle: dict[str, Any], reason: str) -> None:
        exit_cash = exit_fee = unwind_loss = 0.0
        books = self.books([x["token_id"] for x in bundle["legs"].values() if x["filled"] > 0])
        for leg in bundle["legs"].values():
            if leg["filled"] <= 0: continue
            book = books.get(leg["token_id"]); raw = self.market(leg["market_id"])
            if not book or not raw: return
            fee = resolve_fee_details(raw, self.clob, str(raw.get("conditionId") or ""), leg["token_id"])
            walked = walk(book.bids, leg["filled"], buy=False, slippage_bps=float(self.cfg.get("slippage_bps", 5.0)))
            if not fee.verified or not walked: return
            cash = leg["filled"] * walked[1]; f = leg["filled"] * fee_per_share(walked[1], fee, taker=True)
            exit_cash += cash; exit_fee += f; self.state["cash"] += cash - f
        entry = sum(sum(f["shares"] * f["price"] for f in leg["fills"]) for leg in bundle["legs"].values()); entry_fee = sum(sum(f["fee"] for f in leg["fills"]) for leg in bundle["legs"].values())
        unwind_loss = max(0.0, entry + entry_fee - exit_cash + exit_fee)
        self.finalize(bundle, reason, exit_cash, exit_fee, unwind_loss, True)
        bundle["status"] = "UNWOUND"

    def finalize(self, bundle: dict[str, Any], reason: str, cashflow: float, fee: float, unwind_loss: float, unwind_accounted: bool) -> None:
        if bundle.get("final"): return
        entry = sum(sum(f["shares"] * f["price"] + f["fee"] for f in leg["fills"]) for leg in bundle["legs"].values()); first = min((f["receive"] for leg in bundle["legs"].values() for f in leg["fills"]), default=now_ms())
        duration = max(0, now_ms() - first); annual = float((self.cfg.get("v7") or {}).get("capital_cost_rate_annual", .05)); capital_cost = entry * max(0.0, annual) * duration / (365 * 86400 * 1000)
        pnl = cashflow - entry - fee - capital_cost
        self.emit(LedgerEvent(event_type="FINAL", strategy=STRATEGY, model_sha=self.sha, bundle_id=bundle["bundle_id"], event_id=bundle["event_id"], fee=fee, slippage=0.0, unwind_loss=unwind_loss, capital_cost=capital_cost, latency_cost=0.0, realized_cashflow=cashflow, final_pnl=pnl, capital_duration_ms=duration, metadata={"realized": True, "unwind_accounted": unwind_accounted, "reason": reason, "terminal_id": f"graph:{bundle['bundle_id']}:final"}))
        bundle["final"] = True

    def manage(self) -> None:
        now = int(time.time())
        for bundle in self.state["bundles"].values():
            if bundle["status"] in {"CLOSED", "UNWOUND", "CANCELLED"}: continue
            if bundle["status"] in {"RESTING", "PARTIAL"} and now >= bundle["execution_deadline_ts"]:
                if any(x["filled"] > 0 for x in bundle["legs"].values()): bundle["status"] = "ABORTING"; self.unwind(bundle, "execution_timeout")
                else: bundle["status"] = "CANCELLED"; self.finalize(bundle, "no_fill", 0.0, 0.0, 0.0, False)
            elif bundle["status"] == "ABORTING": self.unwind(bundle, str(bundle.get("abort_reason") or "abort"))
            elif bundle["status"] == "COMPLETE":
                payout, resolved = 0.0, True
                for leg in bundle["legs"].values():
                    raw = self.market(leg["market_id"]); yes = resolved_yes(raw) if raw else None
                    if yes is None: resolved = False; break
                    wins = (leg["side"] == "YES" and yes == 1) or (leg["side"] == "NO" and yes == 0); payout += leg["filled"] if wins else 0.0
                if resolved:
                    self.state["cash"] += payout; self.finalize(bundle, "settled", payout, 0.0, 0.0, False); bundle["status"] = "CLOSED"
                elif now >= bundle["hold_deadline_ts"]: self.unwind(bundle, "hold_deadline")

    def markouts(self) -> None:
        books = self.books([leg["token_id"] for b in self.state["bundles"].values() for leg in b["legs"].values() if leg["filled"] > 0])
        current = now_ms()
        for bundle in self.state["bundles"].values():
            for leg in bundle["legs"].values():
                book = books.get(leg["token_id"])
                if not book: continue
                for fill in leg["fills"]:
                    done = set(fill.get("markouts", []))
                    for horizon in (1,10,45,60,300):
                        if horizon in done or current < fill["receive"] + horizon*1000: continue
                        walked = walk(book.bids, fill["shares"], buy=False, slippage_bps=float(self.cfg.get("slippage_bps", 5.0)))
                        if not walked: continue
                        liquidation = fill["shares"] * walked[1]; pnl = liquidation - fill["shares"] * fill["price"]
                        self.emit(LedgerEvent(event_type="MARKOUT", strategy=STRATEGY, model_sha=self.sha, bundle_id=bundle["bundle_id"], order_id=leg["order_id"], fill_id=fill["fill_id"], leg_id=leg["leg_id"], market_id=leg["market_id"], event_id=leg["event_id"], token_id=leg["token_id"], exchange_ts_ms=book.exchange_ts_ms, receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id, executable_liquidation_value=liquidation, markouts={f"{horizon}s": pnl}))
                        fill.setdefault("markouts", []).append(horizon)

    def risk(self) -> None:
        books = self.books([leg["token_id"] for b in self.state["bundles"].values() for leg in b["legs"].values() if leg["filled"] > 0]); equity = float(self.state["cash"])
        for b in self.state["bundles"].values():
            for leg in b["legs"].values():
                if leg["filled"] <= 0: continue
                book = books.get(leg["token_id"]); walked = walk(book.bids, leg["filled"], buy=False, slippage_bps=float(self.cfg.get("slippage_bps", 5.0))) if book else None
                if walked: equity += leg["filled"] * walked[1]
        self.state["peak"] = max(float(self.state.get("peak", equity)), equity); dd = max(0.0, 1 - equity/self.state["peak"]) if self.state["peak"] > 0 else 0.0
        if dd >= float(self.cfg.get("max_drawdown", .15)): self.state["killed"] = True
        atomic_json(self.dir / "status.json", {"schema":"polymarket_v7_graph_rv_status_v1","timestamp":int(time.time()),"paper_only":True,"authenticated_execution":False,"model_sha":self.sha,"cash":self.state["cash"],"equity":equity,"drawdown":dd,"killed":self.state["killed"],"bundle_states":{k:v["status"] for k,v in self.state["bundles"].items()},"contracts":["queue_never_grants_size","unwind_depth_bounds_size","receive_time_causal_fills","shared_trade_capacity","direct_joint_distribution_not_product_of_marginals","partial_unwind_fail_closed","canonical_ledger_spool_only"]})

    def tick(self) -> None:
        self.risk(); self.admit(); self.apply_trades(); self.manage(); self.markouts(); self.risk(); self.state["model_sha"] = self.sha; atomic_json(self.state_path, self.state)


def main() -> int:
    p=argparse.ArgumentParser(description="Native V7 Graph/RV PAPER execution")
    p.add_argument("--config",type=Path,required=True); p.add_argument("--run-root",type=Path,required=True); p.add_argument("--intents",type=Path,required=True); p.add_argument("--trade-tape",type=Path,required=True); p.add_argument("--joint-model",type=Path); p.add_argument("--loop",action="store_true"); p.add_argument("--interval",type=float,default=1.0)
    a=p.parse_args(); broker=Broker(a.config,a.run_root,a.intents,a.trade_tape,a.joint_model)
    while True:
        broker.tick()
        if not a.loop: break
        time.sleep(max(.1,a.interval))
    return 0

if __name__ == "__main__": raise SystemExit(main())
