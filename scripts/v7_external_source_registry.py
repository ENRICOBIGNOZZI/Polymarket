#!/usr/bin/env python3
"""Validate and fingerprint V7 external-information source authority."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROLES = {"EXACT_SETTLEMENT_SOURCE", "SETTLEMENT_OBSERVABILITY_SOURCE", "SEMANTIC_EQUIVALENT_MARKET", "LOGICAL_CONSTRAINT_SOURCE", "CORRELATED_PREDICTOR", "MICROSTRUCTURE_PREDICTOR", "VOLATILITY_PREDICTOR", "CONTEXTUAL_EVENT_SOURCE"}
EVENT_KINDS = {"BOOK_SNAPSHOT", "BOOK_DELTA", "BEST_BID_ASK", "TRADE", "MARK_PRICE", "INDEX_PRICE", "FUNDING", "OPEN_INTEREST", "LIQUIDATION", "OPTION_TICKER", "OPTION_SURFACE", "VOLATILITY_INDEX", "ORACLE_OBSERVATION", "PREDICTION_MARKET_BOOK", "PREDICTION_MARKET_TRADE", "MARKET_METADATA", "SPORTS_STATE", "SPORTS_EVENT", "OFFICIAL_ANNOUNCEMENT", "CORRECTION", "HEARTBEAT", "SOURCE_STATUS"}
REQUIRED = {"source_id", "provider", "venue", "asset", "instrument_id", "instrument_type", "channel", "event_kinds", "role", "transport", "credentials_required", "enabled"}
DENIED_AUTHORITY = {"execution_authority", "capital_authority", "oms_authority", "ledger_writer_authority", "promotion_authority"}


class SourceRegistryError(ValueError):
    pass


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceRegistryError("source_registry_unreadable") from exc
    if not isinstance(value, dict):
        raise SourceRegistryError("source_registry_not_object")
    return value


def validate(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != "polymarket_v7_external_source_registry_v1" or value.get("schema_version") != 1:
        raise SourceRegistryError("source_registry_schema_mismatch")
    if value.get("architecture") != "V7" or value.get("paper_only") is not True:
        raise SourceRegistryError("source_registry_v7_paper_only_required")
    if any(value.get(field) is not False for field in DENIED_AUTHORITY):
        raise SourceRegistryError("source_registry_may_not_have_private_authority")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourceRegistryError("source_registry_sources_required")
    ids: set[str] = set()
    for row in sources:
        if not isinstance(row, dict) or not REQUIRED.issubset(row):
            raise SourceRegistryError("source_registry_source_fields_invalid")
        source_id = row["source_id"]
        if not isinstance(source_id, str) or not source_id or source_id in ids:
            raise SourceRegistryError("source_registry_source_id_invalid")
        ids.add(source_id)
        if row["role"] not in ROLES or not isinstance(row["event_kinds"], list) or not row["event_kinds"]:
            raise SourceRegistryError("source_registry_source_taxonomy_invalid")
        if any(kind not in EVENT_KINDS for kind in row["event_kinds"]):
            raise SourceRegistryError("source_registry_event_kind_invalid")
        if not isinstance(row["credentials_required"], bool) or not isinstance(row["enabled"], bool):
            raise SourceRegistryError("source_registry_source_boolean_invalid")
        if row["credentials_required"] and not isinstance(row.get("required_env"), list):
            raise SourceRegistryError("source_registry_credential_contract_missing")
    aliases = value.get("environment_compatibility")
    if not isinstance(aliases, dict) or any(not key.startswith("PM_V7_") or not isinstance(value, list) for key, value in aliases.items()):
        raise SourceRegistryError("source_registry_environment_compatibility_invalid")
    return {"schema": value["schema"], "source_count": len(sources), "source_ids": sorted(ids), "registry_sha256": _canonical_hash(value)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("config/v7_external_source_registry.json"))
    args = parser.parse_args(argv)
    try:
        print(json.dumps(validate(load(args.registry)), sort_keys=True))
        return 0
    except SourceRegistryError as exc:
        print(f"v7_external_source_registry: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
