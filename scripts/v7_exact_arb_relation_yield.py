#!/usr/bin/env python3
"""Read-only semantic yield diagnostic for a canonical universe snapshot."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
from v7_unified_exact_arb_graph import SAFETY, compile_graph, load, outcome_map, token_map
from v7_exact_relation_discovery import build as discover, identity


def report(universe, registries, model_sha):
    markets = universe.get("markets", [])
    active = [m for m in markets if m.get("active") is True and m.get("closed") is not True]
    reasons = Counter()
    fields = ("binary_partition_verified", "partition_verified", "partition_states", "partition_payout_vectors",
              "neg_risk_complete_set_verified", "settlement_semantic_hash", "normalized_rules_hash",
              "condition_id", "event_id", "fee_schedule", "tick_size", "minimum_order_size")
    for m in active:
        for field in fields:
            if not m.get(field): reasons[field] += 1
    graph = compile_graph(registries + [discover(universe, model_sha)], universe, model_sha)
    families = Counter(r["relation_family"] for r in graph["relations"])
    groups = Counter(identity(m) for m in active if identity(m) is not None)
    negrisk = [m for m in active if m.get("neg_risk") is True]
    transforms = Counter(t["kind"] for t in graph["transformation_registry"].values())
    return {"schema":"polymarket_v7_exact_arb_relation_yield_v1", **SAFETY,
            "model_sha":model_sha, "source_timestamp_ms":universe.get("timestamp_ms"),
            "source_membership_sha256":universe.get("membership_sha256"),
            "evidence_scope":"PUBLIC_CANONICAL_UNIVERSE_METADATA_NOT_LONDON_EXECUTION",
            "markets_total":len(markets), "markets_active":len(active),
            "binary_markets":sum(token_map(m) is not None for m in active),
            "binary_partition_verified":sum(m.get("binary_partition_verified") is True for m in active),
            "nway_markets":sum(len(outcome_map(m) or {}) > 2 for m in active),
            "nway_partition_verified":families["N_WAY_COMPLETE_PARTITION"],
            "negrisk_markets":len(negrisk), "negrisk_events":len({m.get("event_id") for m in negrisk if m.get("event_id")}),
            "negrisk_verified_complete_sets":families["NEGRISK_COMPLETE_SET"],
            "duplicate_identity_groups":sum(n > 1 for n in groups.values()),
            "duplicate_relations":families["AUTOMATIC_PAYOFF_IDENTICAL_DUPLICATE"],
            "explicit_registry_relations":sum(len(r.get("relations", [])) for r in registries),
            "verified_combo_relations":sum(n for f,n in families.items() if "COMBO" in f),
            "verified_merge_relations":transforms["MERGE"],
            "verified_collateral_relations":transforms["COMBO_COLLATERAL_RETURN"],
            "nodes":len(graph["nodes"]), "verified_relations":len(graph["relations"]),
            "relations_by_family":dict(families), "transformations_by_kind":dict(transforms),
            "unverified_candidates":len(graph["unverified_candidates"]),
            "candidate_details":graph["unverified_candidates"],
            "rejected_relations":graph["rejected_relations"],
            "rejection_reasons_by_semantic_field":dict(reasons),
            "graph_generation":graph["graph_generation"]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--universe", type=Path, required=True)
    p.add_argument("--registry", type=Path, action="append", default=[])
    p.add_argument("--model-sha", required=True)
    p.add_argument("--output", type=Path)
    a=p.parse_args()
    result=report(load(a.universe), [load(r) for r in a.registry], a.model_sha)
    body=json.dumps(result, sort_keys=True, indent=2)+"\n"
    if a.output: a.output.write_text(body)
    else: print(body, end="")
    return 0 if result["verified_relations"] else 2


if __name__ == "__main__": raise SystemExit(main())
