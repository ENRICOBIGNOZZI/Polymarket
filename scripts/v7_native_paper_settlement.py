#!/usr/bin/env python3
"""Resolve one closed native PAPER market and append one aggregate FINAL event.

This is cold-plane reconciliation only. It cannot create, cancel, or modify an
order. It derives final cash P&L from the canonical native FILL evidence and
Polymarket's public resolved outcome, then writes through the existing single
ledger spool.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from v7_execution_ledger import native_order_id_matches, LedgerEvent, canonical_ledger_path, iter_events
from v7_ledger_spool import spool_event
from v7_native_settlement_projection import context_from_fill

STATUS_SCHEMA = "polymarket_v7_native_paper_settlement_status_v1"


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def public_json(url: str, timeout: float = 4.0) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-native-settlement/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def native_receipt(event: LedgerEvent) -> dict[str, Any] | None:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    value = metadata.get("native_settlement_receipt")
    if not isinstance(value, dict):
        return None
    if (
        value.get("schema") != "polymarket_v7_native_settlement_receipt_v1"
        or value.get("owner") != "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"
        or value.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or value.get("model_sha") != event.model_sha
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or value.get("execution_mode") != "PAPER_SIMULATED"
        or value.get("single_owner") is not True
        or not native_order_id_matches(event)
    ):
        return None
    return value


def market_events(run_root: Path, model_sha: str, market_id: str) -> list[LedgerEvent]:
    path = canonical_ledger_path(run_root)
    if not path.is_file():
        return []
    return [
        event for event in iter_events(path, expected_model_sha=model_sha)
        if event.strategy == "CRYPTO_SETTLEMENT_ENGINE" and event.market_id == market_id
    ]


def existing_final(events: list[LedgerEvent], market_id: str) -> LedgerEvent | None:
    settlement_id = f"native-settlement:{market_id}"
    for event in events:
        if event.event_type != "FINAL":
            continue
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if metadata.get("native_market_settlement_id") == settlement_id:
            return event
    return None


def aggregate_fills(events: list[LedgerEvent]) -> tuple[dict[str, float], float, list[LedgerEvent]]:
    inventory: dict[str, float] = {}
    cash = 0.0
    fills: list[LedgerEvent] = []
    seen: set[str] = set()
    for event in events:
        if event.event_type != "FILL" or native_receipt(event) is None:
            continue
        if not event.fill_id or event.fill_id in seen:
            raise RuntimeError("native_fill_identity_invalid")
        seen.add(event.fill_id)
        if not event.token_id or event.side not in {"BUY", "SELL"}:
            raise RuntimeError("native_fill_instrument_invalid")
        if event.filled_size is None or event.fill_price is None or event.fee is None:
            raise RuntimeError("native_fill_economics_missing")
        qty = float(event.filled_size)
        price = float(event.fill_price)
        fee = float(event.fee)
        if not all(math.isfinite(x) for x in (qty, price, fee)) or qty <= 0 or not (0 <= price <= 1) or fee < 0:
            raise RuntimeError("native_fill_economics_invalid")
        sign = 1.0 if event.side == "BUY" else -1.0
        inventory[event.token_id] = inventory.get(event.token_id, 0.0) + sign * qty
        cash += (-qty * price - fee) if sign > 0 else (qty * price - fee)
        if inventory[event.token_id] < -1e-9:
            raise RuntimeError("native_naked_sell_in_ledger")
        fills.append(event)
    return inventory, cash, fills


def resolved_outcome(gamma_url: str, market_id: str) -> tuple[str, str] | None:
    raw = public_json(f"{gamma_url.rstrip('/')}/markets/{urllib.parse.quote(market_id)}")
    if not isinstance(raw, dict) or raw.get("closed") is not True:
        return None
    outcomes = [str(x) for x in parse_array(raw.get("outcomes"))]
    tokens = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    prices: list[float] = []
    for value in parse_array(raw.get("outcomePrices")):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            number = math.nan
        prices.append(number)
    winner = next((i for i, p in enumerate(prices) if math.isfinite(p) and p >= 1.0 - 1e-9), -1)
    if winner < 0 or winner >= len(tokens):
        return None
    outcome = outcomes[winner] if winner < len(outcomes) else ""
    return tokens[winner], outcome


def wait_for_record(run_root: Path, model_sha: str, record_id: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = canonical_ledger_path(run_root)
        if path.is_file():
            try:
                if any(event.record_id == record_id for event in iter_events(path, expected_model_sha=model_sha)):
                    return True
            except Exception:
                return False
        time.sleep(0.1)
    return False


def settle(args: argparse.Namespace) -> int:
    status_path = args.run_root / "control" / "native_paper_settlement_status.json"
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        events = market_events(args.run_root, args.model_sha, args.market_id)
        final = existing_final(events, args.market_id)
        if final is not None:
            atomic_json(status_path, {
                "schema": STATUS_SCHEMA, "state": "ALREADY_SETTLED",
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "model_sha": args.model_sha,
                "market_id": args.market_id, "record_id": final.record_id,
                "timestamp_ms": time.time_ns() // 1_000_000,
            })
            return 0

        inventory, cash, fills = aggregate_fills(events)
        if not fills:
            atomic_json(status_path, {
                "schema": STATUS_SCHEMA, "state": "NO_POSITION",
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "model_sha": args.model_sha,
                "market_id": args.market_id, "timestamp_ms": time.time_ns() // 1_000_000,
            })
            return 0

        outcome = resolved_outcome(args.gamma_url, args.market_id)
        if outcome is None:
            if time.monotonic() >= deadline:
                atomic_json(status_path, {
                    "schema": STATUS_SCHEMA, "state": "RESOLUTION_TIMEOUT",
                    "paper_only": True, "authenticated_execution": False,
                    "real_order_submission": False, "model_sha": args.model_sha,
                    "market_id": args.market_id, "timestamp_ms": time.time_ns() // 1_000_000,
                })
                return 79
            time.sleep(2.0)
            continue

        winning_token, resolved_label = outcome
        payout = max(0.0, inventory.get(winning_token, 0.0))
        final_pnl = cash + payout
        if not math.isfinite(final_pnl):
            raise RuntimeError("native_final_pnl_invalid")
        representative = fills[0]
        receipt = native_receipt(representative)
        if receipt is None:
            raise RuntimeError("native_final_receipt_missing")
        settlement_id = f"native-settlement:{args.market_id}"
        metadata = {
            "component": "native_market_settlement",
            "model_family": "native-paper-engine",
            "economic_authority": "PAPER_EXPLORATION",
            "counterfactual": False,
            "research_evidence_only": False,
            "realized": True,
            "unwind_accounted": True,
            "cost_vector_complete": True,
            "native_settlement_receipt": receipt,
            "run_id": representative.metadata.get("run_id"),
            "native_market_settlement_id": settlement_id,
            "winning_token_id": winning_token,
            "settlement_outcome": resolved_label,
            "included_order_ids": sorted({str(event.order_id) for event in fills if event.order_id}),
            "included_fill_ids": sorted({str(event.fill_id) for event in fills if event.fill_id}),
            "included_position_ids": sorted({str(event.position_id) for event in fills if event.position_id}),
            "allocation_basis": "SIGNED_FILL_CASHFLOW_PLUS_SETTLEMENT",
            "crypto_context": context_from_fill({"metadata": representative.metadata}),
            "terminal_id": settlement_id,
            "pnl_decomposition": {
                "trading_cashflow_before_resolution": cash,
                "settlement_payout": payout,
                "trading_pnl": final_pnl,
            },
        }
        final_event = LedgerEvent(
            event_type="FINAL",
            strategy="CRYPTO_SETTLEMENT_ENGINE",
            model_sha=args.model_sha,
            model_version="native-paper-engine",
            order_id=representative.order_id,
            fill_id=representative.fill_id,
            position_id=f"native-market:{args.market_id}",
            market_id=args.market_id,
            event_id=representative.event_id,
            token_id=winning_token,
            final_pnl=final_pnl,
            realized_cashflow=payout,
            fee=0.0,
            slippage=0.0,
            unwind_loss=0.0,
            capital_cost=0.0,
            latency_cost=0.0,
            metadata=metadata,
        )
        spool_event(args.run_root, final_event)
        if not wait_for_record(args.run_root, args.model_sha, final_event.record_id):
            atomic_json(status_path, {
                "schema": STATUS_SCHEMA, "state": "LEDGER_APPEND_TIMEOUT",
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "model_sha": args.model_sha,
                "market_id": args.market_id, "record_id": final_event.record_id,
                "timestamp_ms": time.time_ns() // 1_000_000,
            })
            return 75
        atomic_json(status_path, {
            "schema": STATUS_SCHEMA, "state": "SETTLED",
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "model_sha": args.model_sha,
            "market_id": args.market_id, "record_id": final_event.record_id,
            "fill_count": len(fills), "final_pnl": final_pnl,
            "timestamp_ms": time.time_ns() // 1_000_000,
        })
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--market-id", required=True)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        parser.error("exact lowercase model SHA required")
    if args.timeout_seconds < 1 or args.timeout_seconds > 600:
        parser.error("invalid timeout")
    return args


def main() -> int:
    return settle(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
