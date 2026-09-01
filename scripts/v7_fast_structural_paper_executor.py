#!/usr/bin/env python3
"""Sequential, fail-closed policy evaluator for Fast Structural candidates.

The C++ WebSocket runtime remains the sole detector and shared-state producer.
This worker consumes its canonical candidates, revalidates every leg against a
fresh atomic WebSocket snapshot after an explicit inter-leg delay, and records
orders, fills, unwind and terminal economics through the single ledger spool.
In canonical runtime it runs with ``--shadow-only``: it revalidates arrival
economics but owns no PAPER order, fill, capital, inventory or ledger authority.
The legacy PAPER simulation path remains testable offline and is never launched
by the canonical loop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event
from v7_market_common import finite, request_json
from v7_shared_market_state import SharedStateError, load_snapshot, synchronized_books

SCHEMA = "polymarket_v7_fast_structural_paper_executor_v1"
STRATEGY = "FAST_STRUCTURAL"


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:32]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def emit(run_root: Path, event: LedgerEvent) -> None:
    spool_event(run_root, event)


def fee_per_share(price: float, book: dict[str, Any]) -> float:
    if book.get("fee_verified") is not True:
        return math.inf
    rate = max(0.0, finite(book.get("fee_rate"), math.nan))
    exponent = max(0.0, finite(book.get("fee_exponent"), math.nan))
    if not math.isfinite(rate) or not math.isfinite(exponent) or not 0.0 < price < 1.0:
        return math.inf
    return rate * (price * (1.0 - price)) ** exponent


def walk(levels: list[tuple[float, float]], quantity: float, book: dict[str, Any], *,
         buy: bool, slippage_bps: float, require_full: bool) -> dict[str, float] | None:
    remaining = max(0.0, quantity)
    filled = raw_cash = stressed_cash = fees = 0.0
    stress = max(0.0, slippage_bps) / 10_000.0
    ordered = sorted(levels, key=lambda row: row[0], reverse=not buy)
    for raw_price, visible in ordered:
        take = min(remaining, max(0.0, visible))
        if take <= 0.0:
            continue
        price = min(0.999999, raw_price * (1.0 + stress)) if buy else max(0.000001, raw_price * (1.0 - stress))
        fee = fee_per_share(price, book)
        if not math.isfinite(fee):
            return None
        raw_cash += take * raw_price
        stressed_cash += take * price
        fees += take * fee
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    complete = remaining <= max(1e-9, quantity * 1e-9)
    if filled <= 0.0 or (require_full and not complete):
        return None
    return {
        "filled": filled, "price": stressed_cash / filled, "fee": fees,
        "slippage": abs(stressed_cash - raw_cash), "cash": stressed_cash,
        "net_cash": stressed_cash + fees if buy else stressed_cash - fees,
        "complete": 1.0 if complete else 0.0,
    }


def terminal_metadata(state: str, pnl: float, terminal_id: str) -> dict[str, Any]:
    return {
        "terminal_state": state, "terminal_id": terminal_id,
        "realized": True, "unwind_accounted": True, "cost_vector_complete": True,
        "model_family": STRATEGY,
        "pnl_decomposition": {
            "trading_pnl": pnl, "spread_capture": 0.0, "adverse_markout": 0.0,
            "inventory_pnl": 0.0, "maker_rebates": 0.0,
            "liquidity_rewards": 0.0, "own_reward_share_verified": False,
        },
    }


class Executor:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.run_root = args.run_root
        self.run_dir = self.run_root / "fast_structural"
        self.state_path = self.run_dir / "paper_executor_state.json"
        self.status_path = self.run_dir / "paper_executor_status.json"
        config = load_json(args.config)
        if (config.get("paper_only") is not True
                or (config.get("v7") or {}).get("authenticated_execution") is not False
                or (config.get("v7") or {}).get("real_order_submission") is not False):
            raise RuntimeError("fast_structural_executor_requires_safe_paper_config")
        policy_path = getattr(args, "policy", Path("config/v7_fast_structural.json"))
        self.policy = load_json(policy_path)
        if (self.policy.get("paper_only") is not True
                or self.policy.get("authenticated_execution") is not False
                or self.policy.get("real_order_submission") is not False):
            raise RuntimeError("fast_structural_policy_requires_safe_paper_config")
        self.gamma_url = str(config.get("gamma_url") or "").rstrip("/")
        self.shadow_only = bool(getattr(args, "shadow_only", False))
        start = max(0.0, finite(config.get("starting_capital"), 0.0))
        prior = load_json(self.state_path)
        if prior.get("model_sha") != args.model_sha:
            prior = {}
        self.state: dict[str, Any] = {
            "model_sha": args.model_sha, "starting_capital": start, "cash": start,
            "ledger_offset": 0, "seen_candidates": [], "open_bundles": {},
            "aborting_bundles": {}, "realized_pnl_total": 0.0,
            "candidates_seen": 0, "entries": 0, "rejections": {},
            "shadow_qualified": 0,
            **prior,
        }
        self.state["model_sha"] = args.model_sha
        self.state["starting_capital"] = start
        if self.shadow_only and (
            self.state.get("open_bundles") or self.state.get("aborting_bundles")
        ):
            raise RuntimeError("shadow_mode_refuses_prior_paper_inventory")

    def save(self) -> None:
        self.state["seen_candidates"] = list(self.state.get("seen_candidates") or [])[-50_000:]
        atomic_json(self.state_path, self.state)

    def reject(self, reason: str) -> None:
        reasons = self.state.setdefault("rejections", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def snapshot_books(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        snapshot = load_snapshot(
            self.args.shared_state, expected_sha=self.args.model_sha,
            max_publish_age_ms=self.args.max_shared_publish_age_ms,
        )
        return synchronized_books(snapshot, tokens, require_continuous=True)

    def read_candidates(self) -> list[LedgerEvent]:
        path = self.run_root / "ledger" / "execution.jsonl"
        if not path.exists():
            return []
        size = path.stat().st_size
        offset = min(max(0, int(self.state.get("ledger_offset") or 0)), size)
        output: list[LedgerEvent] = []
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    handle.seek(start)
                    break
                offset = handle.tell()
                try:
                    event = LedgerEvent.from_dict(json.loads(line))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    self.reject("CANONICAL_LEDGER_RECORD_INVALID")
                    continue
                if event.event_type == "CANDIDATE" and event.strategy == STRATEGY:
                    output.append(event)
        self.state["ledger_offset"] = offset
        return output

    def candidate_legs(self, event: LedgerEvent) -> list[dict[str, Any]]:
        raw = event.metadata.get("structured_legs") if isinstance(event.metadata, dict) else None
        if not isinstance(raw, list) or not raw:
            return []
        legs: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                return []
            token = str(item.get("token_id") or "")
            leg_id = str(item.get("leg_id") or token)
            quantity = finite(item.get("target_quantity"), math.nan)
            if not token or not leg_id or not math.isfinite(quantity) or quantity <= 0.0:
                return []
            legs.append({**item, "token_id": token, "leg_id": leg_id, "target_quantity": quantity})
        return legs

    def record_feasibility(
        self, event: LedgerEvent, legs: list[dict[str, Any]], bundle_id: str,
    ) -> dict[str, Any]:
        """Record full-depth economics without granting execution authority."""
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        tokens = [str(leg["token_id"]) for leg in legs]
        payoff_floor = max(0.0, finite(metadata.get("payoff_floor"), 0.0))
        opportunity_id = str(event.opportunity_id or f"fast-feasibility-{bundle_id}")
        feasibility: dict[str, Any] = {
            "schema": "polymarket_v7_fast_structural_feasibility_observation_v1",
            "detected": True, "structurally_valid": bool(
                metadata.get("hard_arbitrage") is True
                and metadata.get("execution_compatibility") == "SEQUENTIAL_FOK_HARD_ARB"
                and legs and payoff_floor > 0.0),
            "full_depth_positive": False, "positive_after_fees": False,
            "positive_after_latency": False, "capacity_curve": [],
            "q_star": None, "tau_star_ms": None,
            "execution_authority": "SHADOW_ZERO_AUTHORITY",
        }
        try:
            books = self.snapshot_books(tokens)
        except SharedStateError as exc:
            feasibility["reason"] = type(exc).__name__.upper()
            books = {}
        if books and feasibility["structurally_valid"]:
            target = min(float(leg["target_quantity"]) for leg in legs)
            quantities = sorted({
                quantity for quantity in (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, target)
                if quantity > 0.0
            })
            latency_bps = max(0.0, finite(
                self.policy.get("latency_penalty_bps"), 0.0))
            latency_span_ms = max(1.0, float(self.args.leg_latency_ms) * max(1, len(legs) - 1))
            max_notional = max(0.0, finite(
                self.policy.get("max_notional_usd"), 0.0))
            for quantity in quantities:
                plans = []
                for leg in legs:
                    book = books[str(leg["token_id"])]
                    plan = walk(
                        book["asks"], quantity, book, buy=True,
                        slippage_bps=self.args.slippage_bps, require_full=True,
                    )
                    if plan is None:
                        plans = []
                        break
                    plans.append(plan)
                if not plans:
                    feasibility["capacity_curve"].append({
                        "quantity": quantity, "full_depth_complete": False,
                    })
                    continue
                payout = quantity * payoff_floor
                raw_cost = sum(plan["cash"] - plan["slippage"] for plan in plans)
                fees = sum(plan["fee"] for plan in plans)
                stressed_cost = sum(plan["net_cash"] for plan in plans)
                latency_cost = payout * latency_bps / 10_000.0 * max(1, len(legs) - 1)
                within_notional = 0.0 < stressed_cost <= max_notional + 1e-12
                row = {
                    "quantity": quantity, "full_depth_complete": True,
                    "within_finite_notional_limit": within_notional,
                    "payout_floor": payout, "raw_cost": raw_cost, "fees": fees,
                    "slippage": stressed_cost - raw_cost - fees,
                    "latency_cost": latency_cost,
                    "gross_edge": payout - raw_cost,
                    "net_after_fees": payout - raw_cost - fees,
                    "net_after_slippage": payout - stressed_cost,
                    "net_after_latency": payout - stressed_cost - latency_cost,
                }
                feasibility["capacity_curve"].append(row)
            target_row = min(
                feasibility["capacity_curve"],
                key=lambda row: abs(float(row["quantity"]) - target),
            )
            if target_row.get("full_depth_complete") is True:
                feasibility["full_depth_positive"] = target_row["gross_edge"] > 0.0
                feasibility["positive_after_fees"] = target_row["net_after_fees"] > 0.0
                feasibility["positive_after_latency"] = bool(
                    target_row["net_after_latency"] > 0.0
                    and target_row["within_finite_notional_limit"])
            positive = [
                row for row in feasibility["capacity_curve"]
                if row.get("full_depth_complete") is True
                and row.get("within_finite_notional_limit") is True
                and finite(row.get("net_after_latency"), -math.inf) > 0.0
            ]
            feasibility["q_star"] = max(
                (float(row["quantity"]) for row in positive), default=None)
            base = finite(target_row.get("net_after_slippage"), 0.0)
            baseline_latency_cost = finite(target_row.get("latency_cost"), 0.0)
            if base > 0.0 and baseline_latency_cost > 0.0:
                feasibility["tau_star_ms"] = base / (
                    baseline_latency_cost / latency_span_ms)
            feasibility["latency_model"] = {
                "method": "CONFIGURED_LINEAR_PENALTY_DIAGNOSTIC",
                "baseline_span_ms": latency_span_ms,
                "latency_penalty_bps": latency_bps,
            }
            feasibility["books"] = {
                token: {
                    "bids": book.get("bids") or [], "asks": book.get("asks") or [],
                    "exchange_ts_ms": book.get("exchange_ts_ms"),
                    "receive_ts_ms": book.get("received_ms"),
                    "snapshot_id": book.get("bus_snapshot_id"),
                    "fee_verified": book.get("fee_verified"),
                    "fee_rate": book.get("fee_rate"),
                    "fee_exponent": book.get("fee_exponent"),
                }
                for token, book in books.items()
            }
        emit(self.run_root, LedgerEvent(
            event_type="OPPORTUNITY", strategy=STRATEGY,
            model_sha=self.args.model_sha, opportunity_id=opportunity_id,
            candidate_id=str(event.candidate_id or event.record_id), bundle_id=bundle_id,
            event_id=str(event.event_id or ""), expected_ev=event.expected_ev,
            metadata={
                "fast_structural_feasibility": feasibility,
                "counterfactual": True,
                "economic_authority": "SHADOW_COUNTERFACTUAL",
                "execution_authority": "SHADOW_ZERO_AUTHORITY",
                "excluded_from_portfolio_equity": True,
            },
        ))
        return feasibility

    def execute_candidate(self, event: LedgerEvent) -> None:
        seen = set(str(value) for value in self.state.get("seen_candidates") or [])
        identity = str(event.candidate_id or event.record_id)
        if identity in seen:
            return
        self.state.setdefault("seen_candidates", []).append(identity)
        self.state["candidates_seen"] = int(self.state.get("candidates_seen") or 0) + 1
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        legs = self.candidate_legs(event)
        bundle_id = str(event.bundle_id or f"fast-paper-{identity}")
        if (metadata.get("hard_arbitrage") is not True
                or metadata.get("execution_compatibility") != "SEQUENTIAL_FOK_HARD_ARB" or not legs
                or finite(metadata.get("payoff_floor"), 0.0) <= 0.0):
            self.reject("UNSUPPORTED_OR_UNSTRUCTURED_CANDIDATE")
            self.save()
            return
        feasibility = self.record_feasibility(event, legs, bundle_id)
        if not feasibility.get("positive_after_latency"):
            self.reject("FEASIBILITY_NET_EDGE_NONPOSITIVE")
            self.save()
            return
        if bundle_id in self.state.get("open_bundles", {}) or bundle_id in self.state.get("aborting_bundles", {}):
            self.reject("BUNDLE_ALREADY_OWNED")
            self.save()
            return
        required = {str(leg["leg_id"]): float(leg["target_quantity"]) for leg in legs}
        estimated = max(0.0, finite(metadata.get("capital_required"), 0.0))
        cash = max(0.0, finite(self.state.get("cash"), 0.0))
        if estimated <= 0.0 or estimated > cash + 1e-9:
            self.reject("INSUFFICIENT_SLEEVE_CAPITAL")
            self.save()
            return
        bundle = {
            "bundle_id": bundle_id, "candidate_id": identity,
            "event_id": str(event.event_id or ""), "opportunity_id": str(event.opportunity_id or ""),
            "payoff_floor": float(metadata["payoff_floor"]), "legs": [],
            "required": required, "opened_ms": 0, "basis": 0.0,
            "realized_unwind_pnl": 0.0,
        }
        redemption_quantity = min(required.values())
        payout_floor = redemption_quantity * float(bundle["payoff_floor"])
        for index, leg in enumerate(legs):
            if index:
                time.sleep(max(0.0, self.args.leg_latency_ms / 1000.0))
            # Reprice *every remaining leg* from one atomic shared-state
            # generation before exposing the first dollar, and repeat after
            # each inter-leg delay. Per-leg FOK sufficiency is not enough: a
            # complete-set basket can have two individually fillable legs whose
            # combined arrival cost already exceeds its deterministic payout.
            # The previous implementation discovered that only after buying
            # every leg and then paid the bid/ask spread again to unwind.
            remaining_legs = legs[index:]
            remaining_tokens = [str(item["token_id"]) for item in remaining_legs]
            try:
                books = self.snapshot_books(remaining_tokens)
            except SharedStateError:
                self.reject("STALE_OR_INCOHERENT_SHARED_STATE")
                break

            remaining_plans: list[tuple[dict[str, Any], dict[str, Any], dict[str, float]]] = []
            for remaining_leg in remaining_legs:
                remaining_token = str(remaining_leg["token_id"])
                book = books[remaining_token]
                plan = walk(
                    book["asks"], float(remaining_leg["target_quantity"]), book,
                    buy=True, slippage_bps=self.args.slippage_bps, require_full=True,
                )
                if plan is None:
                    remaining_plans = []
                    break
                remaining_plans.append((remaining_leg, book, plan))
            if not remaining_plans:
                self.reject("LEG_FOK_REVALIDATION_FAILED")
                break
            remaining_cost = sum(float(item[2]["net_cash"]) for item in remaining_plans)
            projected_basis = float(bundle["basis"]) + remaining_cost
            if payout_floor <= projected_basis + 1e-12:
                self.reject("ARRIVAL_BUNDLE_NET_EDGE_NONPOSITIVE")
                break
            if remaining_cost > float(self.state["cash"]) + 1e-9:
                self.reject("INSUFFICIENT_SLEEVE_CAPITAL_AT_ARRIVAL")
                break

            _, book, planned = remaining_plans[0]
            token = str(leg["token_id"])
            target = float(leg["target_quantity"])
            # Once a first fill is about to be journaled, persist ownership in
            # the aborting lane so any later reprice failure must unwind rather
            # than disappear. A candidate rejected before this point never
            # manufactures an empty bundle or a terminal unit.
            if bundle_id not in self.state.get("aborting_bundles", {}):
                self.state.setdefault("aborting_bundles", {})[bundle_id] = bundle
                self.save()
            decision_ms = time.time_ns() // 1_000_000
            order_id = f"fast-order-{stable_id(bundle_id, leg['leg_id'], decision_ms)}"
            fill_id = f"fast-fill-{stable_id(order_id, book['bus_snapshot_id'])}"
            shared = {
                "model_family": STRATEGY, "source_candidate_id": identity,
                "source_detector": "FAST_STRUCTURAL_CPP_WEBSOCKET",
                "target_quantities": required, "opportunity_kind": metadata.get("opportunity_kind"),
                "execution_state": "SEQUENTIAL_FOK", "cost_vector_complete": False,
            }
            emit(self.run_root, LedgerEvent(
                event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=self.args.model_sha,
                candidate_id=identity, bundle_id=bundle_id, order_id=order_id,
                leg_id=str(leg["leg_id"]), market_id=str(leg.get("market_id") or book.get("market_id") or ""),
                event_id=str(event.event_id or book.get("event_id") or ""), token_id=token,
                side="BUY", exchange_ts_ms=int(book["exchange_ts_ms"]),
                receive_ts_ms=int(book["received_ms"]), decision_ts_ms=decision_ms,
                book_snapshot_id=str(book["bus_snapshot_id"]), limit_price=float(planned["price"]),
                intended_action="SEQUENTIAL_FOK_BUY", intended_size=target,
                order_state="SUBMITTED_PAPER", metadata=shared,
            ))
            emit(self.run_root, LedgerEvent(
                event_type="FILL", strategy=STRATEGY, model_sha=self.args.model_sha,
                candidate_id=identity, bundle_id=bundle_id, order_id=order_id, fill_id=fill_id,
                leg_id=str(leg["leg_id"]), market_id=str(leg.get("market_id") or book.get("market_id") or ""),
                event_id=str(event.event_id or book.get("event_id") or ""), token_id=token,
                side="BUY", exchange_ts_ms=int(book["exchange_ts_ms"]),
                receive_ts_ms=int(book["received_ms"]), fill_price=float(planned["price"]),
                filled_size=target, complete=True, fee=float(planned["fee"]),
                fee_rate=float(book["fee_rate"]), fee_source="SHARED_STATE_VERIFIED_FEE",
                slippage=float(planned["slippage"]), metadata=shared,
            ))
            actual = {
                **leg, "order_id": order_id, "fill_id": fill_id,
                "shares": target, "entry_price": float(planned["price"]),
                "basis": float(planned["net_cash"]), "entry_fee": float(planned["fee"]),
                "entry_slippage": float(planned["slippage"]),
            }
            bundle["legs"].append(actual)
            bundle["basis"] = float(bundle["basis"]) + float(planned["net_cash"])
            self.state["cash"] = float(self.state["cash"]) - float(planned["net_cash"])
            self.save()
        if len(bundle["legs"]) == len(legs):
            if payout_floor <= float(bundle["basis"]) + 1e-12:
                self.reject("ARRIVAL_NET_EDGE_NONPOSITIVE")
            else:
                bundle["opened_ms"] = time.time_ns() // 1_000_000
                self.state.setdefault("open_bundles", {})[bundle_id] = bundle
                self.state.get("aborting_bundles", {}).pop(bundle_id, None)
                self.state["entries"] = int(self.state.get("entries") or 0) + 1
        self.save()

    def evaluate_candidate_shadow(self, event: LedgerEvent) -> None:
        seen = set(str(value) for value in self.state.get("seen_candidates") or [])
        identity = str(event.candidate_id or event.record_id)
        if identity in seen:
            return
        self.state.setdefault("seen_candidates", []).append(identity)
        self.state["candidates_seen"] = int(self.state.get("candidates_seen") or 0) + 1
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        legs = self.candidate_legs(event)
        if (
            metadata.get("hard_arbitrage") is not True
            or metadata.get("execution_compatibility") != "SEQUENTIAL_FOK_HARD_ARB"
            or not legs
            or finite(metadata.get("payoff_floor"), 0.0) <= 0.0
        ):
            self.reject("UNSUPPORTED_OR_UNSTRUCTURED_CANDIDATE")
            return
        bundle_id = str(event.bundle_id or f"fast-shadow-{identity}")
        feasibility = self.record_feasibility(event, legs, bundle_id)
        if feasibility.get("positive_after_latency") is not True:
            self.reject("FEASIBILITY_NET_EDGE_NONPOSITIVE")
            return
        self.state["shadow_qualified"] = int(self.state.get("shadow_qualified") or 0) + 1

    def unwind(self) -> None:
        for bundle_id, bundle in list((self.state.get("aborting_bundles") or {}).items()):
            residual: list[dict[str, Any]] = []
            for leg in list(bundle.get("legs") or []):
                token = str(leg.get("token_id") or "")
                shares = max(0.0, finite(leg.get("shares"), 0.0))
                basis = max(0.0, finite(leg.get("basis"), 0.0))
                try:
                    book = self.snapshot_books([token])[token]
                    filled = walk(book["bids"], shares, book, buy=False,
                                  slippage_bps=self.args.slippage_bps, require_full=False)
                except SharedStateError:
                    filled = None
                if filled is None:
                    residual.append(leg)
                    continue
                fraction = min(1.0, float(filled["filled"]) / shares)
                allocated_basis = basis * fraction
                proceeds = float(filled["net_cash"])
                pnl = proceeds - allocated_basis
                decision_ms = time.time_ns() // 1_000_000
                order_id = f"fast-unwind-{stable_id(bundle_id, token, decision_ms)}"
                fill_id = f"fast-unwind-fill-{stable_id(order_id, book['bus_snapshot_id'])}"
                common = dict(
                    strategy=STRATEGY, model_sha=self.args.model_sha, bundle_id=bundle_id,
                    order_id=order_id, leg_id=str(leg.get("leg_id") or token),
                    market_id=str(leg.get("market_id") or book.get("market_id") or ""),
                    event_id=str(bundle.get("event_id") or book.get("event_id") or ""), token_id=token,
                    side="SELL", exchange_ts_ms=int(book["exchange_ts_ms"]),
                    receive_ts_ms=int(book["received_ms"]),
                    metadata={"model_family": STRATEGY, "terminal_path": "PARTIAL_BUNDLE_UNWIND",
                              "target_quantities": bundle.get("required") or {}},
                )
                emit(self.run_root, LedgerEvent(
                    event_type="ORDER_SUBMITTED", decision_ts_ms=decision_ms,
                    book_snapshot_id=str(book["bus_snapshot_id"]), limit_price=float(filled["price"]),
                    intended_action="UNWIND", intended_size=float(filled["filled"]),
                    order_state="CROSS_PAPER", **common,
                ))
                emit(self.run_root, LedgerEvent(
                    event_type="FILL", fill_id=fill_id, fill_price=float(filled["price"]),
                    filled_size=float(filled["filled"]), complete=fraction >= 1.0 - 1e-9,
                    fee=float(filled["fee"]), fee_rate=float(book["fee_rate"]),
                    fee_source="SHARED_STATE_VERIFIED_FEE", slippage=float(filled["slippage"]),
                    unwind_loss=max(0.0, -pnl), **common,
                ))
                self.state["cash"] = float(self.state["cash"]) + proceeds
                bundle["realized_unwind_pnl"] = float(bundle.get("realized_unwind_pnl") or 0.0) + pnl
                remaining = max(0.0, shares - float(filled["filled"]))
                if remaining > 1e-9:
                    residual.append({**leg, "shares": remaining, "basis": basis - allocated_basis})
            bundle["legs"] = residual
            bundle["basis"] = sum(float(leg.get("basis") or 0.0) for leg in residual)
            if not residual:
                pnl = float(bundle.get("realized_unwind_pnl") or 0.0)
                self.state["realized_pnl_total"] = float(self.state.get("realized_pnl_total") or 0.0) + pnl
                emit(self.run_root, LedgerEvent(
                    event_type="FINAL", strategy=STRATEGY, model_sha=self.args.model_sha,
                    bundle_id=bundle_id, event_id=str(bundle.get("event_id") or ""),
                    final_pnl=pnl, realized_cashflow=pnl, fee=0.0, slippage=0.0,
                    unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
                    capital_duration_ms=max(0, time.time_ns() // 1_000_000 - int(bundle.get("opened_ms") or time.time_ns() // 1_000_000)),
                    metadata=terminal_metadata("PARTIAL_BUNDLE_UNWOUND", pnl, f"fast:{bundle_id}:final"),
                ))
                self.state.get("aborting_bundles", {}).pop(bundle_id, None)
            self.save()

    def settle(self) -> None:
        for bundle_id, bundle in list((self.state.get("open_bundles") or {}).items()):
            event_id = str(bundle.get("event_id") or "")
            if not event_id:
                continue
            try:
                raw = request_json(f"{self.gamma_url}/events/{event_id}", timeout=4)
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("closed") is not True:
                continue
            quantity = min(float(leg["shares"]) for leg in bundle.get("legs") or [])
            payout = quantity * float(bundle.get("payoff_floor") or 0.0)
            basis = float(bundle.get("basis") or 0.0)
            pnl = payout - basis
            self.state["cash"] = float(self.state["cash"]) + payout
            self.state["realized_pnl_total"] = float(self.state.get("realized_pnl_total") or 0.0) + pnl
            emit(self.run_root, LedgerEvent(
                event_type="FINAL", strategy=STRATEGY, model_sha=self.args.model_sha,
                bundle_id=bundle_id, position_id=bundle_id, event_id=event_id,
                final_pnl=pnl, realized_cashflow=pnl, fee=0.0, slippage=0.0,
                unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
                capital_duration_ms=max(0, time.time_ns() // 1_000_000 - int(bundle["opened_ms"])),
                metadata=terminal_metadata("STRUCTURAL_PAYOUT", pnl, f"fast:{bundle_id}:final"),
            ))
            self.state.get("open_bundles", {}).pop(bundle_id, None)
            self.save()

    def publish(self) -> None:
        open_bundles = self.state.get("open_bundles") or {}
        aborting = self.state.get("aborting_bundles") or {}
        locked = sum(float(bundle.get("basis") or 0.0) for bundle in open_bundles.values())
        abort_basis = sum(float(bundle.get("basis") or 0.0) for bundle in aborting.values())
        equity = float(self.state.get("cash") or 0.0) + locked + abort_basis
        atomic_json(self.status_path, {
            "schema": SCHEMA, "timestamp": int(time.time()), "model_sha": self.args.model_sha,
            "state": "RUNNING", "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "single_oms": True,
            "shadow_only": self.shadow_only,
            "execution_authority": "SHADOW_ZERO_AUTHORITY" if self.shadow_only else "PAPER",
            "capital_authority": False if self.shadow_only else True,
            "ledger_writer_authority": False if self.shadow_only else True,
            "observational_spool_authority": self.shadow_only,
            "source_detector": "FAST_STRUCTURAL_CPP_WEBSOCKET",
            "cash": float(self.state.get("cash") or 0.0), "equity": equity,
            "equity_source": "cost_basis_fail_closed",
            "realized_pnl_total": float(self.state.get("realized_pnl_total") or 0.0),
            "open_bundles": len(open_bundles), "aborting_bundles": len(aborting),
            "candidates_seen": int(self.state.get("candidates_seen") or 0),
            "entries": int(self.state.get("entries") or 0),
            "shadow_qualified": int(self.state.get("shadow_qualified") or 0),
            "rejections": self.state.get("rejections") or {}, "killed": False,
        })

    def step(self) -> None:
        if self.shadow_only:
            for candidate in self.read_candidates():
                self.evaluate_candidate_shadow(candidate)
        else:
            self.unwind()
            self.settle()
            for candidate in self.read_candidates():
                self.execute_candidate(candidate)
        self.save()
        self.publish()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path("config/v7_fast_structural.json"))
    parser.add_argument("--shared-state", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--leg-latency-ms", type=int, default=100)
    parser.add_argument("--max-shared-publish-age-ms", type=int, default=2500)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--shadow-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise RuntimeError("exact_model_sha_required")
    executor = Executor(args)
    while True:
        executor.step()
        if args.once:
            return 0
        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
