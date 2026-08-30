#!/usr/bin/env python3
"""Build verified Graph/RV relations from canonical venue metadata.

Only deterministic same-event NegRisk membership is executable. Textual
similarity is intentionally absent from this builder.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
from pathlib import Path
import time
from typing import Any

SCHEMA = "polymarket_v7_verified_relation_registry_v1"


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "id", "type", "antecedent_market_id", "consequent_market_id", "enabled"))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def proof_hash(event_id: str, members: list[dict[str, Any]]) -> str:
    facts = [{
        "market_id": str(row.get("market_id") or ""),
        "condition_id": str(row.get("condition_id") or ""),
        "tokens": list(row.get("clob_token_ids") or []),
        "outcomes": list(row.get("outcomes") or []),
        "neg_risk": row.get("neg_risk") is True,
        "resolution_source": str(row.get("resolution_source") or ""),
    } for row in sorted(members, key=lambda value: str(value.get("market_id") or ""))]
    raw = json.dumps({"event_id": event_id, "members": facts}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def build(universe: dict[str, Any], *, model_sha: str, now_ms: int) -> dict[str, Any]:
    if universe.get("schema") != "polymarket_v7_adaptive_universe_snapshot_v1":
        raise ValueError("universe:schema")
    if universe.get("model_sha") != model_sha or universe.get("paper_only") is not True:
        raise ValueError("universe:identity_or_safety")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in universe.get("markets", []):
        if not isinstance(row, dict) or row.get("neg_risk") is not True:
            continue
        if not row.get("condition_id") or len(row.get("clob_token_ids") or []) < 2:
            continue
        for event_id in row.get("event_ids", []):
            if str(event_id):
                grouped.setdefault(str(event_id), []).append(row)

    verified: list[dict[str, Any]] = []
    for event_id, raw_members in sorted(grouped.items()):
        members = list({str(row["market_id"]): row for row in raw_members}.values())
        if len(members) < 2:
            continue
        rules_hash = proof_hash(event_id, members)
        for left, right in itertools.combinations(sorted(members, key=lambda row: str(row["market_id"])), 2):
            relation_id = "negrisk-mutex-" + hashlib.sha256(
                f"{event_id}|{left['market_id']}|{right['market_id']}|{rules_hash}".encode()
            ).hexdigest()[:20]
            verified.append({
                "relation_id": relation_id,
                "relation_type": "MUTUAL_EXCLUSION",
                "antecedent_market_id": str(left["market_id"]),
                "consequent_market_id": str(right["market_id"]),
                "event_id": event_id,
                "antecedent_condition_id": str(left["condition_id"]),
                "consequent_condition_id": str(right["condition_id"]),
                "antecedent_tokens": list(left["clob_token_ids"]),
                "consequent_tokens": list(right["clob_token_ids"]),
                "rules_hash": rules_hash,
                "proof_source": "canonical_gamma_event_membership_and_negrisk_flag",
                "verifier_version": "v7-negrisk-deterministic-1",
                "generated_at_ms": now_ms,
                "expires_at_ms": now_ms + 86_400_000,
                "confidence": 1.0,
                "authority": "VERIFIED_DETERMINISTIC",
                "settlement_compatibility": "SAME_NEGRISK_EVENT",
                "fee_schedules": {
                    str(left["market_id"]): left.get("fee_schedule") or {},
                    str(right["market_id"]): right.get("fee_schedule") or {},
                },
                "executable": True,
            })
    return {
        "schema": SCHEMA,
        "timestamp_ms": now_ms,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "universe_membership_sha256": universe.get("membership_sha256"),
        "candidate_relations": len(verified),
        "verified_relations": len(verified),
        "text_similarity_confers_authority": False,
        "relations": verified,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--runtime-csv", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    args = parser.parse_args()
    universe = json.loads(args.universe.read_text(encoding="utf-8"))
    registry = build(universe, model_sha=args.model_sha, now_ms=time.time_ns() // 1_000_000)
    atomic_json(args.registry, registry)
    atomic_csv(args.runtime_csv, [{
        "id": row["relation_id"], "type": row["relation_type"],
        "antecedent_market_id": row["antecedent_market_id"],
        "consequent_market_id": row["consequent_market_id"], "enabled": "true",
    } for row in registry["relations"]])
    print(json.dumps({key: registry[key] for key in (
        "schema", "model_sha", "candidate_relations", "verified_relations")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
