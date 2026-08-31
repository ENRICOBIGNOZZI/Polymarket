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
from pathlib import Path
from typing import Any


class DriftError(ValueError):
    pass


ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriftError(f"invalid_json:{path}") from exc
    if not isinstance(value, dict):
        raise DriftError("object_required")
    return value


def validate_registry(registry: dict[str, Any]) -> None:
    required = {"schema_version", "execution_mode_on_drift", "last_verified_at", "official_sources", "api", "contracts", "protocol", "market_constraints"}
    if set(registry) != required or registry.get("schema_version") != 1 or registry.get("execution_mode_on_drift") not in {"SHADOW_LIVE_READ_ONLY", "CANCEL_ONLY"}:
        raise DriftError("registry_shape")
    if not isinstance(registry["official_sources"], list) or not registry["official_sources"] or any(not isinstance(url, str) or not url.startswith("https://docs.polymarket.com/") for url in registry["official_sources"]):
        raise DriftError("registry_sources")
    api, contracts, protocol, constraints = registry["api"], registry["contracts"], registry["protocol"], registry["market_constraints"]
    if not isinstance(api, dict) or api != {"version": "CLOB_V2", "production_url": "https://clob.polymarket.com", "sdk_package": "@polymarket/clob-client-v2", "chain_id": 137}:
        raise DriftError("registry_api")
    expected_contracts = {"pUSD", "pUSD_decimals", "conditional_tokens", "ctf_exchange", "neg_risk_ctf_exchange", "neg_risk_adapter"}
    if not isinstance(contracts, dict) or set(contracts) != expected_contracts or contracts.get("pUSD_decimals") != 6 or any(not isinstance(contracts[name], str) or not ADDRESS.fullmatch(contracts[name]) for name in expected_contracts - {"pUSD_decimals"}):
        raise DriftError("registry_contracts")
    if not isinstance(protocol, dict) or protocol.get("matching_engine_restart_status") != 425 or protocol.get("post_restart_post_only_seconds") != 120 or protocol.get("order_heartbeat_endpoint") != "/heartbeats" or protocol.get("order_heartbeat_required") is not True:
        raise DriftError("registry_protocol")
    if not isinstance(constraints, dict) or not all(constraints.get(name) is True for name in {"fee_source_required", "tick_size_source_required", "minimum_order_size_source_required", "settlement_rule_hash_required", "websocket_schema_hash_required", "rate_limit_source_required"}):
        raise DriftError("registry_constraints")


def compare(registry: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Compare only explicit values supplied by the archival collector.

    Missing observed values are drift: silence cannot authorize execution.
    """
    keys = {"api.version": registry["api"]["version"], "api.production_url": registry["api"]["production_url"], "api.chain_id": registry["api"]["chain_id"], "contracts.pUSD": registry["contracts"]["pUSD"], "contracts.pUSD_decimals": registry["contracts"]["pUSD_decimals"], "contracts.ctf_exchange": registry["contracts"]["ctf_exchange"], "contracts.neg_risk_ctf_exchange": registry["contracts"]["neg_risk_ctf_exchange"], "contracts.neg_risk_adapter": registry["contracts"]["neg_risk_adapter"], "protocol.order_heartbeat_endpoint": registry["protocol"]["order_heartbeat_endpoint"], "protocol.matching_engine_restart_status": registry["protocol"]["matching_engine_restart_status"]}
    drift: list[str] = []
    for dotted, expected in keys.items():
        top, child = dotted.split(".")
        if not isinstance(observed.get(top), dict) or observed[top].get(child) != expected:
            drift.append(dotted)
    return drift


def report(registry: dict[str, Any], snapshot_path: Path) -> dict[str, Any]:
    observed = load(snapshot_path)
    drift = compare(registry, observed)
    return {"schema_version": 1, "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(), "drift": drift, "status": "HEALTHY" if not drift else "DRIFT", "required_execution_mode": "PAPER_SIMULATED" if not drift else registry["execution_mode_on_drift"]}


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
