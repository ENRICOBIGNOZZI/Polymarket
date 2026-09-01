#!/usr/bin/env python3
"""Validate the zero-authority V7 research and external-data plane."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_research_data_plane_v1"
AUTHORITIES = {"capital", "oms", "inventory", "ledger", "orders", "promotion"}
ABLATIONS = {"all_sources", "minus_one_source", "stale_or_degraded_source", "mapping_uncertainty"}
RETENTION = {
    "unique_causal_information", "necessary_settlement_truth",
    "execution_capacity_latency_evidence", "material_operational_diagnostics",
}
STATUS = {"IMPLEMENTED", "PARTIAL", "NOT_APPLICABLE"}
FORBIDDEN_SOURCE_NEEDLES = (
    "from v7_" + "ledger_spool import", "import v7_" + "ledger_spool",
    "spool_" + "event(", "from v7_execution_ledger import " + "LedgerEvent",
)


class ResearchContractError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ResearchContractError("json_object_required")
    return value


def validate(root: Path, contract: dict[str, Any], authority: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    if (
        contract.get("schema") != SCHEMA
        or contract.get("version") != 1
        or contract.get("paper_only") is not True
        or contract.get("authenticated_execution") is not False
        or contract.get("real_order_submission") is not False
        or contract.get("real_capital_at_risk") is not False
        or contract.get("automatic_promotion") is not False
        or contract.get("authority_registry") != "config/v7_authority_registry.json"
        or set(contract.get("forbidden_authorities") or []) != AUTHORITIES
        or set(contract.get("required_ablations") or []) != ABLATIONS
    ):
        raise ResearchContractError("contract_identity_or_safety")
    credentials = contract.get("credential_policy")
    if not isinstance(credentials, dict) or credentials != {
        "trading_token_allowed": False,
        "public_collectors_require_public_or_read_only_credentials": True,
        "authenticated_read_only_collectors_must_use_separate_nontrading_credentials": True,
    }:
        raise ResearchContractError("credential_policy")
    families = contract.get("families")
    expected = set(authority.get("research_zero_authority_families") or [])
    if not isinstance(families, dict) or set(families) != expected:
        raise ResearchContractError("research_family_partition")
    if set(scope.get("research_zero_authority_families") or []) != expected:
        raise ResearchContractError("live_scope_family_partition")
    process_owners: dict[str, str] = {}
    partial_mappings: list[str] = []
    for family, row in families.items():
        if not isinstance(row, dict):
            raise ResearchContractError(f"family_shape:{family}")
        if not str(row.get("unique_purpose") or "").strip():
            raise ResearchContractError(f"unique_purpose:{family}")
        if row.get("retention_basis") not in RETENTION:
            raise ResearchContractError(f"retention_basis:{family}")
        if set(row.get("ablations") or []) != ABLATIONS:
            raise ResearchContractError(f"ablations:{family}")
        if row.get("authorities") != {key: False for key in sorted(AUTHORITIES)}:
            raise ResearchContractError(f"authority:{family}")
        if not str(row.get("feature_schema") or "").strip() or not str(row.get("provenance") or "").strip():
            raise ResearchContractError(f"schema_or_provenance:{family}")
        for field in ("causality_status", "source_health_status", "immutable_tape_status"):
            if row.get(field) not in STATUS:
                raise ResearchContractError(f"{field}:{family}")
        mapping = row.get("mapping_status")
        if not isinstance(mapping, dict) or set(mapping) != {"market", "entity", "settlement"} or any(
            value not in STATUS for value in mapping.values()
        ):
            raise ResearchContractError(f"mapping_status:{family}")
        if "PARTIAL" in mapping.values():
            partial_mappings.append(family)
        processes = row.get("processes")
        outputs = row.get("outputs")
        if not isinstance(processes, list) or not processes or not isinstance(outputs, list) or not outputs:
            raise ResearchContractError(f"process_or_output:{family}")
        for name in processes:
            if not isinstance(name, str) or name in process_owners:
                raise ResearchContractError(f"process_unique:{name}")
            path = root / name
            if not path.is_file():
                raise ResearchContractError(f"process_missing:{name}")
            source = path.read_text(encoding="utf-8")
            if any(needle in source for needle in FORBIDDEN_SOURCE_NEEDLES):
                raise ResearchContractError(f"executable_api_reachable:{name}")
            process_owners[name] = family
        if row.get("credential_class") not in {
            "PUBLIC_READ_ONLY", "SEPARATE_AUTHENTICATED_READ_ONLY_OR_PUBLIC",
        }:
            raise ResearchContractError(f"credential_class:{family}")
    return {
        "schema": "polymarket_v7_research_data_plane_validation_v1",
        "paper_only": True,
        "family_count": len(families),
        "process_count": len(process_owners),
        "all_authorities_false": True,
        "trading_token_allowed": False,
        "partial_mapping_families": sorted(partial_mappings),
        "gate_passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--contract", type=Path, default=Path("config/v7_research_data_plane.json"))
    parser.add_argument("--authority", type=Path, default=Path("config/v7_authority_registry.json"))
    parser.add_argument("--scope", type=Path, default=Path("config/v7_live_model_scope.json"))
    args = parser.parse_args()
    try:
        root = args.repository_root.resolve()
        result = validate(root, _load(root / args.contract), _load(root / args.authority), _load(root / args.scope))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, ResearchContractError) as exc:
        print(f"v7_research_data_plane_contract: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
