#!/usr/bin/env python3
"""Build one exact-SHA PAPER config for the native PureArb multi-market runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_native_crypto_engine_manager import (  # noqa: E402
    _close_unix,
    fee_parameters,
    select_markets,
    tick_size_e4,
    venue_minimum_microunits,
)

SCHEMA = "polymarket_v7_pure_arb_multi_runtime_v1"
DEFAULT_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_MARKETS = 64


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def stable_handle(domain: str, value: str) -> int:
    digest = hashlib.sha256(f"{domain}:{value}".encode()).digest()
    handle = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
    return handle or 1


def _usd_micro(value: Any, name: str) -> int:
    try:
        raw = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name}_invalid") from exc
    if not (raw > 0.0) or raw == float("inf") or raw != raw:
        raise ValueError(f"{name}_invalid")
    return int(round(raw * 1_000_000.0))


def capital_limits(allocation: dict[str, Any]) -> dict[str, int]:
    if (
        allocation.get("paper_only") is not True
        or (allocation.get("v7") or {}).get("authenticated_execution") is not False
        or (allocation.get("v7") or {}).get("real_order_submission") is not False
    ):
        raise ValueError("allocation_not_paper_only")
    scope = allocation.get("capital_scope")
    if not isinstance(scope, dict):
        raise ValueError("capital_scope_missing")
    if (
        scope.get("scope_class") != "ENGINE_ENVELOPE"
        or scope.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or scope.get("independent_capital_authority") is not False
        or scope.get("independent_risk_authority") is not False
        or scope.get("independent_oms_authority") is not False
    ):
        raise ValueError("capital_scope_invalid")
    total_usd = float(allocation.get("starting_capital") or 0.0)
    if total_usd <= 0:
        raise ValueError("starting_capital_invalid")
    max_gross = float(allocation.get("max_gross_fraction") or 0.0)
    max_market = float(allocation.get("max_market_fraction") or 0.0)
    max_trade = float(allocation.get("max_trade_usd") or 0.0)
    if min(max_gross, max_market, max_trade) <= 0:
        raise ValueError("capital_limit_invalid")
    sleeve = _usd_micro(total_usd, "sleeve")
    return {
        "sleeve_budget_microdollars": sleeve,
        "max_total_exposure_microdollars": min(
            sleeve, _usd_micro(total_usd * max_gross, "max_total")),
        "max_market_exposure_microdollars": min(
            sleeve, _usd_micro(total_usd * max_market, "max_market")),
        "max_single_order_microdollars": min(
            sleeve, _usd_micro(max_trade, "max_order")),
    }


def outcome_tokens(row: dict[str, Any]) -> tuple[str, str]:
    tokens = [str(x) for x in row.get("clob_token_ids") or []]
    outcomes = [str(x).strip().upper() for x in row.get("outcomes") or []]
    if len(tokens) != 2 or len(outcomes) != 2 or not all(tokens):
        raise ValueError("binary_market_mapping_invalid")
    mapping = dict(zip(outcomes, tokens))
    if set(mapping) == {"YES", "NO"}:
        return mapping["YES"], mapping["NO"]
    if set(mapping) == {"UP", "DOWN"}:
        return mapping["UP"], mapping["DOWN"]
    raise ValueError(f"unsupported_binary_outcomes:{outcomes}")


def default_terms(row: dict[str, Any], yes_token: str, no_token: str) -> dict[str, Any]:
    yes_tick = tick_size_e4(yes_token)
    no_tick = tick_size_e4(no_token)
    minimum = max(
        venue_minimum_microunits(yes_token),
        venue_minimum_microunits(no_token),
    )
    rate, exponent, source = fee_parameters(row)
    return {
        "yes_tick_size_e4": yes_tick,
        "no_tick_size_e4": no_tick,
        "minimum_order_microunits": minimum,
        "fee_rate": rate,
        "fee_exponent": exponent,
        "fee_source": source,
    }


def build_config(
    snapshot: dict[str, Any],
    allocation: dict[str, Any],
    model_sha: str,
    latency_tape: str,
    *,
    pm_ws_url: str = DEFAULT_WS,
    terms_resolver: Callable[[dict[str, Any], str, str], dict[str, Any]] = default_terms,
) -> dict[str, Any]:
    if len(model_sha) != 40 or any(c not in "0123456789abcdef" for c in model_sha):
        raise ValueError("exact_model_sha_required")
    selected = select_markets(snapshot, model_sha)
    if not selected:
        raise ValueError("no_active_registered_markets")
    if len(selected) > MAX_MARKETS:
        raise ValueError("market_capacity_exceeded")

    markets: list[dict[str, Any]] = []
    seen_handles: set[int] = set()
    for context, row in sorted(selected.items()):
        yes_token, no_token = outcome_tokens(row)
        terms = terms_resolver(row, yes_token, no_token)
        start_s = int(row.get("window_start_unix") or 0)
        close_s = _close_unix(row)
        event_ids = [str(x) for x in row.get("event_ids") or [] if str(x)]
        market_id = str(row.get("market_id") or "")
        if start_s <= 0 or close_s <= start_s or not market_id or not event_ids:
            raise ValueError(f"market_identity_invalid:{context}")
        handles = {
            "market_handle": stable_handle("market", market_id),
            "event_handle": stable_handle("event", event_ids[0]),
            "yes_instrument_handle": stable_handle("instrument", yes_token),
            "no_instrument_handle": stable_handle("instrument", no_token),
        }
        for value in handles.values():
            if value in seen_handles:
                raise ValueError("handle_collision")
            seen_handles.add(value)
        minimum = int(terms["minimum_order_microunits"])
        yes_tick = int(terms["yes_tick_size_e4"])
        no_tick = int(terms["no_tick_size_e4"])
        if minimum <= 0 or yes_tick <= 0 or no_tick <= 0:
            raise ValueError(f"market_terms_invalid:{context}")
        markets.append({
            **handles,
            "market_start_wall_ms": start_s * 1000,
            "market_end_wall_ms": close_s * 1000,
            "maximum_leg_skew_ns": 100_000_000,
            "minimum_order_microunits": minimum,
            "fee_rate": float(terms["fee_rate"]),
            "fee_exponent": float(terms["fee_exponent"]),
            "reserve_per_share": 0.0005,
            "fee_verified": True,
            "yes_token": yes_token,
            "no_token": no_token,
            "yes_tick_size_e4": yes_tick,
            "no_tick_size_e4": no_tick,
            # Fresh bounded PAPER validation starts flat. BUY complete-set fills
            # create inventory in-process; SELL is fail-closed until prefunded.
            "yes_inventory_microunits": 0,
            "no_inventory_microunits": 0,
            "yes_collateral_basis_microdollars": 0,
            "no_collateral_basis_microdollars": 0,
        })

    return {
        "schema": SCHEMA,
        "version": 1,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "model_sha": model_sha,
        "pm_ws_url": pm_ws_url,
        "latency_tape": latency_tape,
        "capital_limits": capital_limits(allocation),
        "markets": markets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--allocation", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--latency-tape", required=True)
    parser.add_argument("--pm-ws-url", default=DEFAULT_WS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = json.loads(args.universe.read_text(encoding="utf-8"))
    allocation = json.loads(args.allocation.read_text(encoding="utf-8"))
    value = build_config(
        snapshot, allocation, args.model_sha, args.latency_tape,
        pm_ws_url=args.pm_ws_url,
    )
    atomic_json(args.output, value)
    print(json.dumps({
        "schema": "polymarket_v7_pure_arb_multi_config_status_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": args.model_sha,
        "market_count": len(value["markets"]),
        "output": str(args.output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
