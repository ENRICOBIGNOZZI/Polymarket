#!/usr/bin/env python3
"""Validate the pinned Polymarket CLOB V2/pUSD contract registry.

This is an offline configuration guard, not a signer or RPC client. It makes a
legacy-contract or wrong-chain deployment fail before any live capability can
be considered.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_clob_v2_contract_registry_v1"
ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
REQUIRED_CLOB = {"production_url", "user_websocket_url", "l1_api_key_path", "l2_signature_scheme", "exchange_eip712_domain_version", "api_auth_domain_version", "ctf_exchange", "neg_risk_ctf_exchange", "conditional_tokens"}
REQUIRED_COLLATERAL = {"asset", "decimals", "underlying_asset", "underlying_proxy", "proxy", "implementation", "onramp", "offramp", "ctf_collateral_adapter", "neg_risk_ctf_collateral_adapter"}


class ContractRegistryError(ValueError):
    pass


def registry_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractRegistryError("registry:not_object")
    required = {"schema", "chain_id", "paper_only", "authenticated_execution", "real_order_submission", "source", "verified_at", "clob", "collateral", "operations"}
    if set(value) != required or value["schema"] != SCHEMA:
        raise ContractRegistryError("registry:shape_or_schema")
    if value["chain_id"] != 137:
        raise ContractRegistryError("registry:polygon_mainnet_required")
    if value["paper_only"] is not True or value["authenticated_execution"] is not False or value["real_order_submission"] is not False:
        raise ContractRegistryError("registry:checked_in_live_authority_forbidden")
    if value["source"] != "https://docs.polymarket.com/resources/contracts":
        raise ContractRegistryError("registry:source")
    clob = value["clob"]
    collateral = value["collateral"]
    if not isinstance(clob, dict) or set(clob) != REQUIRED_CLOB:
        raise ContractRegistryError("clob:shape")
    if not isinstance(collateral, dict) or set(collateral) != REQUIRED_COLLATERAL:
        raise ContractRegistryError("collateral:shape")
    if (clob["production_url"] != "https://clob.polymarket.com"
            or clob["user_websocket_url"] != "wss://ws-subscriptions-clob.polymarket.com/ws/user"
            or clob["l1_api_key_path"] != "/auth/api-key"
            or clob["l2_signature_scheme"] != "HMAC-SHA256(base64Decode(secret), timestamp + method + path + body)"
            or clob["exchange_eip712_domain_version"] != "2"
            or clob["api_auth_domain_version"] != "1"):
        raise ContractRegistryError("clob:v2_identity")
    if (collateral["asset"] != "pUSD" or collateral["decimals"] != 6
            or collateral["underlying_asset"] != "USDCe"):
        raise ContractRegistryError("collateral:pusd_identity")
    for name, address in {**{key: clob[key] for key in REQUIRED_CLOB if key.endswith("exchange") or key == "conditional_tokens"}, **{key: collateral[key] for key in REQUIRED_COLLATERAL if key not in {"asset", "decimals", "underlying_asset"}}}.items():
        if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
            raise ContractRegistryError(f"address:{name}")
    if len(set(collateral[key] for key in REQUIRED_COLLATERAL if key not in {"asset", "decimals", "underlying_asset"})) != 7:
        raise ContractRegistryError("collateral:duplicate_contract")
    operations = value["operations"]
    if not isinstance(operations, dict) or set(operations) != {"split", "merge", "redeem", "pUSD_wrap", "pUSD_unwrap"}:
        raise ContractRegistryError("operations:shape")
    return value


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractRegistryError("registry:unreadable") from exc
    return validate(value)
