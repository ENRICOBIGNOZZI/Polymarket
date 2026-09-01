#!/usr/bin/env python3
"""Validate an archived official V7 platform-contract snapshot.

The monitor deliberately does not guess missing fee, market, or contract fields.
It accepts a retrieved JSON snapshot as input, hashes its exact bytes, and fails
closed to the configured read-only mode whenever the selected fields drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DriftError(ValueError):
    pass


ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
PROTOCOL_FIELDS = {"l1_auth", "l2_auth", "exchange_eip712_domain_version", "order_heartbeat_endpoint",
                   "order_heartbeat_required", "matching_engine_restart_status", "post_restart_post_only_seconds",
                   "authenticated_orders_endpoint", "authenticated_trades_endpoint", "authenticated_pagination"}
CONSTRAINT_FIELDS = {"fee_source_required", "tick_size_source_required", "minimum_order_size_source_required",
                     "settlement_rule_hash_required", "websocket_schema_hash_required", "rate_limit_source_required"}
DYNAMIC_MARKET_CONTRACT = {
    "exchange_type": {"source": "CLOB_ORDERBOOK_NEG_RISK", "value_required": True},
    "negative_risk": {"source": "CLOB_ORDERBOOK_OR_MARKET_INFO", "value_required": True},
    "tick_size": {"source": "CLOB_MARKET_INFO", "value_required": True},
    "minimum_order_size": {"source": "CLOB_MARKET_INFO", "value_required": True},
    "fee_schedule": {"source": "CLOB_MARKET_INFO", "snapshot_hash_required": True},
    "reward_schedule": {"source": "CLOB_MARKET_INFO", "snapshot_hash_required": True},
    "taker_delay": {"source": "CLOB_MARKET_INFO", "snapshot_hash_required": True},
    "settlement_rule": {"source": "PER_MARKET_ARCHIVED_SOURCE", "oracle_hash_required": True,
                          "rule_hash_required": True},
    "market_websocket": {"endpoint": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                          "schema_hash_required": True},
    "rate_limit": {"source": "RESPONSE_HEADERS", "tier_header": "Poly-RateLimit-Tier", "tier_required": True},
}
DATA_API_CONTRACT = {"activity_endpoint": "https://data-api.polymarket.com/activity", "activity_pagination": "offset",
                     "activity_timestamp_windows_required": True, "activity_sort_by": "TIMESTAMP",
                     "activity_sort_direction": "ASC", "activity_max_limit": 500}


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftError(f"invalid_json:{path}") from exc
    if not isinstance(value, dict):
        raise DriftError("object_required")
    return value


def timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise DriftError(f"{field}:invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DriftError(f"{field}:invalid") from exc
    if parsed.tzinfo is None:
        raise DriftError(f"{field}:timezone")
    return parsed.astimezone(timezone.utc)


def validate_registry(registry: dict[str, Any]) -> None:
    required = {"schema_version", "execution_mode_on_drift", "max_snapshot_age_seconds", "last_verified_at", "official_sources", "api", "contracts", "protocol", "market_contract", "data_api", "market_constraints"}
    if set(registry) != required or registry.get("schema_version") != 1 or registry.get("execution_mode_on_drift") not in {"SHADOW_LIVE_READ_ONLY", "CANCEL_ONLY"}:
        raise DriftError("registry_shape")
    if not isinstance(registry["official_sources"], list) or not registry["official_sources"] or any(not isinstance(url, str) or not url.startswith("https://docs.polymarket.com/") for url in registry["official_sources"]):
        raise DriftError("registry_sources")
    if (isinstance(registry["max_snapshot_age_seconds"], bool) or not isinstance(registry["max_snapshot_age_seconds"], int)
            or registry["max_snapshot_age_seconds"] <= 0):
        raise DriftError("registry_snapshot_age")
    timestamp(registry["last_verified_at"], "registry_last_verified_at")
    api, contracts, protocol, market_contract, data_api, constraints = registry["api"], registry["contracts"], registry["protocol"], registry["market_contract"], registry["data_api"], registry["market_constraints"]
    if not isinstance(api, dict) or api != {"version": "CLOB_V2", "production_url": "https://clob.polymarket.com", "sdk_package": "@polymarket/clob-client-v2", "chain_id": 137}:
        raise DriftError("registry_api")
    expected_contracts = {"pUSD", "pUSD_decimals", "conditional_tokens", "ctf_exchange", "neg_risk_ctf_exchange", "ctf_collateral_adapter", "neg_risk_ctf_collateral_adapter"}
    canonical_contracts = {
        "pUSD": "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb", "pUSD_decimals": 6,
        "conditional_tokens": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
        "ctf_exchange": "0xe111180000d2663c0091e4f400237545b87b996b",
        "neg_risk_ctf_exchange": "0xe2222d279d744050d28e00520010520000310f59",
        "ctf_collateral_adapter": "0xada100db00ca00073811820692005400218fce1f",
        "neg_risk_ctf_collateral_adapter": "0xada2005600dec949baf300f4c6120000bdb6eaab",
    }
    if (not isinstance(contracts, dict) or set(contracts) != expected_contracts
            or any(not isinstance(contracts[name], str) or not ADDRESS.fullmatch(contracts[name])
                   for name in expected_contracts - {"pUSD_decimals"})
            or contracts != canonical_contracts):
        raise DriftError("registry_contracts")
    expected_protocol = {"l1_auth": "EIP-712 ClobAuthDomain version 1", "l2_auth": "HMAC-SHA256",
                         "exchange_eip712_domain_version": "2", "order_heartbeat_endpoint": "/heartbeats",
                         "order_heartbeat_required": True, "matching_engine_restart_status": 425,
                         "post_restart_post_only_seconds": 120,
                         "authenticated_orders_endpoint": "/data/orders",
                         "authenticated_trades_endpoint": "/data/trades",
                         "authenticated_pagination": "cursor"}
    if not isinstance(protocol, dict) or set(protocol) != PROTOCOL_FIELDS or protocol != expected_protocol:
        raise DriftError("registry_protocol")
    if not isinstance(market_contract, dict) or market_contract != DYNAMIC_MARKET_CONTRACT:
        raise DriftError("registry_market_contract")
    if not isinstance(data_api, dict) or data_api != DATA_API_CONTRACT:
        raise DriftError("registry_data_api")
    if not isinstance(constraints, dict) or set(constraints) != CONSTRAINT_FIELDS or not all(constraints[name] is True for name in CONSTRAINT_FIELDS):
        raise DriftError("registry_constraints")


def compare(registry: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Compare only explicit values supplied by the archival collector.

    Missing observed values are drift: silence cannot authorize execution.
    """
    keys = {
        **{f"api.{key}": value for key, value in registry["api"].items()},
        **{f"contracts.{key}": value for key, value in registry["contracts"].items()},
        **{f"protocol.{key}": value for key, value in registry["protocol"].items()},
        **{f"market_contract.{key}": value for key, value in registry["market_contract"].items()},
        **{f"data_api.{key}": value for key, value in registry["data_api"].items()},
        **{f"market_constraints.{key}": value for key, value in registry["market_constraints"].items()},
    }
    drift: list[str] = []
    for dotted, expected in keys.items():
        top, child = dotted.split(".")
        if not isinstance(observed.get(top), dict) or observed[top].get(child) != expected:
            drift.append(dotted)
    return drift


def report(registry: dict[str, Any], snapshot_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    validate_registry(registry)
    observed = load(snapshot_path)
    drift = compare(registry, observed)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise DriftError("report_now:timezone")
    instant = instant.astimezone(timezone.utc)
    observed_at: datetime | None = None
    try:
        observed_at = timestamp(observed.get("observed_at"), "snapshot_observed_at")
        if observed_at > instant or (instant - observed_at).total_seconds() > registry["max_snapshot_age_seconds"]:
            drift.append("snapshot.observed_at")
    except DriftError:
        drift.append("snapshot.observed_at")
    return {"schema_version": 1, "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            "snapshot_observed_at": observed.get("observed_at"), "drift": sorted(set(drift)),
            "status": "HEALTHY" if not drift else "DRIFT",
            "required_execution_mode": "PAPER_SIMULATED" if not drift else registry["execution_mode_on_drift"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=Path("config/v7_platform_contract.json"))
    parser.add_argument("--snapshot", type=Path, required=True, help="Exact archival collector response; never a hand-edited summary.")
    args = parser.parse_args()
    registry = load(args.registry)
    validate_registry(registry)
    print(json.dumps(report(registry, args.snapshot), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
