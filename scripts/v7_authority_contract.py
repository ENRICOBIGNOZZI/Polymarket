#!/usr/bin/env python3
"""Validate the one-owner V7 economic authority contract.

This is a static fail-closed control. Runtime single-writer evidence remains a
separate required proof and cannot be inferred from this checked-in registry.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_authority_registry_v2"
OWNER_KEYS = (
    "global_portfolio_coordinator",
    "capital_allocator",
    "risk_engine",
    "oms",
    "inventory",
    "ledger",
    "promotion",
    "runtime_identity",
)
EXPECTED_OWNERS = {
    "global_portfolio_coordinator": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
    "capital_allocator": "V7_CANONICAL_ALLOCATOR",
    "risk_engine": "V7_CANONICAL_RISK",
    "oms": "V7_CANONICAL_OMS",
    "inventory": "V7_CANONICAL_INVENTORY",
    "ledger": "V7_CANONICAL_LEDGER",
    "promotion": "V7_OPERATOR_EXACT_SHA_PROMOTION",
    "runtime_identity": "V7_EXACT_SHA_RUNTIME_IDENTITY",
}
ENGINE_COMPONENTS = {
    "CRYPTO_SETTLEMENT_ENGINE": {
        "crypto_settlement_fair", "crypto_informed_taker", "professional_maker",
    },
    "STRUCTURAL_ARB_ENGINE": {"hard_arb", "fast_structural"},
}
ENGINE_ACTIONS = {
    "CRYPTO_SETTLEMENT_ENGINE": {"MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING"},
    "STRUCTURAL_ARB_ENGINE": {"ARB", "CANCEL", "NOTHING"},
}


class AuthorityContractError(ValueError):
    pass


def validate(value: dict[str, Any]) -> dict[str, Any]:
    if (
        value.get("schema") != SCHEMA
        or value.get("version") != 2
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or value.get("automatic_promotion") is not False
    ):
        raise AuthorityContractError("identity_or_safety")
    owners = value.get("owners")
    if not isinstance(owners, dict) or set(owners) != set(OWNER_KEYS):
        raise AuthorityContractError("owner_partition")
    for key, expected in EXPECTED_OWNERS.items():
        owner = owners.get(key)
        if not isinstance(owner, str) or not owner or owner != expected:
            raise AuthorityContractError(f"owner_count_or_identity:{key}")
    engines = value.get("live_algorithms")
    if not isinstance(engines, dict) or set(engines) != set(ENGINE_COMPONENTS):
        raise AuthorityContractError("economic_engine_partition")
    all_components: set[str] = set()
    for engine_id, expected_components in ENGINE_COMPONENTS.items():
        row = engines.get(engine_id)
        if not isinstance(row, dict):
            raise AuthorityContractError(f"economic_engine_shape:{engine_id}")
        components = set(row.get("components") or [])
        actions = set(row.get("actions") or [])
        if components != expected_components or actions != ENGINE_ACTIONS[engine_id]:
            raise AuthorityContractError(f"economic_engine_contract:{engine_id}")
        if all_components & components:
            raise AuthorityContractError("component_has_multiple_engine_owners")
        all_components |= components
    if value.get("component_independent_authority") is not False:
        raise AuthorityContractError("component_independent_authority")
    expected_chain = [
        "LIVE_ALGORITHM", EXPECTED_OWNERS["global_portfolio_coordinator"],
        EXPECTED_OWNERS["capital_allocator"], EXPECTED_OWNERS["risk_engine"],
        EXPECTED_OWNERS["oms"], EXPECTED_OWNERS["inventory"],
        EXPECTED_OWNERS["ledger"],
    ]
    if value.get("decision_chain") != expected_chain:
        raise AuthorityContractError("decision_chain")
    return {
        "schema": "polymarket_v7_authority_audit_v1",
        "passed": True,
        "owner_counts": {key: 1 for key in OWNER_KEYS},
        "economic_engine_count": len(engines),
        "component_count": len(all_components),
        "legacy_algorithm_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry", type=Path, default=Path("config/v7_authority_registry.json"),
    )
    args = parser.parse_args()
    try:
        raw = json.loads(args.registry.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise AuthorityContractError("registry_not_object")
        print(json.dumps(validate(raw), sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, AuthorityContractError) as exc:
        print(f"v7_authority_contract: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
