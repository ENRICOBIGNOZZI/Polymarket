#!/usr/bin/env python3
"""Compile the non-causal warm hotset into a causal observer selection.

The output is only a subscription description for the existing zero-authority
C++ market observer.  It never promotes REST screen evidence into an
opportunity.  Every selected relation must resolve to complete canonical binary
market pairs in the same immutable graph/universe generation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from v7_unified_exact_arb_graph import SAFETY, GraphError, load, safe, validate_graph
from v7_exact_arb_source_health import universe_lease, graph_lease, lease, HOTSET_MAX_AGE_MS
from v7_exact_arb_native_compile import compile_runtime_bundle

SCHEMA = "polymarket_v7_exact_arb_hotset_selection_v1"
STATUS_SCHEMA = "polymarket_v7_exact_arb_hotset_selection_status_v1"
HOTSET_SCHEMA = "polymarket_v7_exact_arb_hotset_v1"
UNIVERSE_SCHEMA = "polymarket_v7_exact_arb_exchange_universe_v1"
EVIDENCE = "NONATOMIC_PUBLIC_REST_SCREEN_ONLY"


def atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def pair_tokens(market: dict[str, Any]) -> tuple[str, str] | None:
    tokens = market.get("clob_token_ids")
    outcomes = market.get("outcomes")
    if not isinstance(tokens, list) or not isinstance(outcomes, list) or len(tokens) != 2 or len(outcomes) != 2:
        return None
    mapping = {str(outcomes[i]).strip().upper(): str(tokens[i]).strip() for i in range(2)}
    yes = mapping.get("YES") or mapping.get("UP")
    no = mapping.get("NO") or mapping.get("DOWN")
    if not yes or not no or yes == no:
        return None
    return yes, no


def compile_selection(
    graph: dict[str, Any],
    universe: dict[str, Any],
    hotset: dict[str, Any],
    model_sha: str,
    max_markets: int,
    *, as_of_ms: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_graph(graph, model_sha)
    if (
        universe.get("schema") != UNIVERSE_SCHEMA
        or not safe(universe, model_sha)
        or universe.get("execution_authority") is not False
    ):
        raise GraphError("hotset_universe_identity")
    if (
        hotset.get("schema") != HOTSET_SCHEMA
        or not safe(hotset, model_sha)
        or hotset.get("execution_authority") is not False
        or hotset.get("actionable") is not False
        or hotset.get("evidence_quality") != EVIDENCE
        or hotset.get("selection_purpose") != "CAUSAL_HOT_OBSERVATION_PRIORITY_ONLY"
    ):
        raise GraphError("hotset_identity")
    if hotset.get("graph_generation") != graph.get("graph_generation"):
        raise GraphError("hotset_generation")
    if graph.get("source_universe_membership_sha256") != universe.get("membership_sha256"):
        raise GraphError("hotset_universe_generation")
    expiry = None
    if as_of_ms is not None:
        expiry = min(universe_lease(universe, as_of_ms), graph_lease(graph, as_of_ms),
                     lease(hotset.get("timestamp_ms"), as_of_ms, HOTSET_MAX_AGE_MS, "hotset"))

    relation_by_id = {str(row.get("relation_id")): row for row in graph.get("relations") or []}
    market_by_id = {str(row.get("market_id")): row for row in universe.get("markets") or []}
    chosen_relations = []
    chosen_markets: set[str] = set()
    rejected: dict[str, int] = {}

    for ranked in hotset.get("relations") or []:
        rid = str(ranked.get("relation_id") or "")
        relation = relation_by_id.get(rid)
        if relation is None or relation.get("enabled") is not True:
            rejected["relation_missing_or_disabled"] = rejected.get("relation_missing_or_disabled", 0) + 1
            continue
        market_ids = sorted({str(leg.get("market_id") or "") for leg in relation.get("legs") or []})
        if not market_ids or any(not mid for mid in market_ids):
            rejected["relation_market_identity_missing"] = rejected.get("relation_market_identity_missing", 0) + 1
            continue
        resolved = []
        valid = True
        for mid in market_ids:
            market = market_by_id.get(mid)
            pair = pair_tokens(market) if market is not None else None
            if market is None or pair is None or market.get("binary_partition_verified") is not True:
                valid = False
                break
            try:
                start_ms = int(market.get("window_start_unix") or 0) * 1000
                end_ms = int(market.get("close_timestamp_unix") or 0) * 1000
            except (TypeError, ValueError):
                valid = False
                break
            if start_ms <= 0 or end_ms <= start_ms:
                valid = False
                break
            resolved.append((market, pair, start_ms, end_ms))
        if not valid:
            rejected["market_not_causal_observer_ready"] = rejected.get("market_not_causal_observer_ready", 0) + 1
            continue
        new_ids = chosen_markets | set(market_ids)
        if len(new_ids) > max_markets:
            rejected["hotset_market_capacity"] = rejected.get("hotset_market_capacity", 0) + 1
            continue
        chosen_markets = new_ids
        chosen_relations.append((relation, resolved))

    selection_rows: dict[str, dict[str, Any]] = {}
    selected_relation_ids = []
    for relation, resolved in chosen_relations:
        selected_relation_ids.append(str(relation["relation_id"]))
        for market, (yes, no), start_ms, end_ms in resolved:
            mid = str(market["market_id"])
            selection_rows[mid] = {
                "market_id": mid,
                "event_id": str(market.get("event_id") or ""),
                "condition_id": market.get("condition_id"),
                "yes_token": yes,
                "no_token": no,
                "start_timestamp_ms": start_ms,
                "end_timestamp_ms": end_ms,
                # Operational labels only; settlement identity remains in graph.
                "asset": str(market.get("asset") or "POLYMARKET"),
                "horizon": str(market.get("horizon") or "EVENT"),
                "fee_schedule": market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {},
                "fees_enabled": bool(market.get("fees_enabled", False)),
                "fees_enabled_explicit": market.get("fees_enabled_explicit") is True,
                "tick_size": market.get("tick_size"),
                "minimum_order_size": market.get("minimum_order_size"),
            }
    rows = sorted(selection_rows.values(), key=lambda row: row["market_id"])
    # Publication is one atomic selection file: membership, proof-carrying
    # native operands and source expiration cannot belong to different graphs.
    native_bundle = compile_runtime_bundle(
        graph, model_sha, list(dict.fromkeys(selected_relation_ids)),
        [token for row in rows for token in (row["yes_token"], row["no_token"])],
    )
    identity = {
        "graph_generation": graph["graph_generation"],
        "markets": [
            (row["market_id"], row["event_id"], row["yes_token"], row["no_token"])
            for row in rows
        ],
    }
    membership = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    selection = {
        "schema": SCHEMA,
        "source_valid": expiry is not None,
        "timestamp_ms": as_of_ms,
        "valid_until_ms": expiry,
        "version": 1,
        **SAFETY,
        "execution_authority": False,
        "selection_only": True,
        "model_sha": model_sha,
        "graph_generation": graph["graph_generation"],
        "source_universe_membership_sha256": universe["membership_sha256"],
        "selection_membership_sha256": membership,
        "selection_purpose": "CAUSAL_HOT_OBSERVATION_PRIORITY_ONLY",
        "source_evidence_quality": EVIDENCE,
        "source_actionable": False,
        "selected_relation_ids": selected_relation_ids,
        "native_runtime_bundle": native_bundle,
        "markets": rows,
    }
    status = {
        "schema": STATUS_SCHEMA,
        **SAFETY,
        "execution_authority": False,
        "model_sha": model_sha,
        "state": "READY" if rows else "EMPTY",
        "graph_generation": graph["graph_generation"],
        "selection_membership_sha256": membership,
        "selected_relations": len(selected_relation_ids),
        "selected_markets": len(rows),
        "selected_tokens": 2 * len(rows),
        "rejection_reasons": rejected,
        "timestamp_ms": time.time_ns() // 1_000_000,
    }
    return selection, status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--universe", type=Path, required=True)
    ap.add_argument("--hotset", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--max-markets", type=int, default=64)
    ap.add_argument("--interval-seconds", type=float, default=1.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--collect-venue-terms", action="store_true", help="Collect public terms off-path; invalidates native selection while refreshing")
    ap.add_argument("--venue-terms-directory", type=Path, help="Immutable receipt archive; use graph_hotset/native_venue_terms beside native_generations")
    args = ap.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("invalid model sha")
    if not (1 <= args.max_markets <= 64 and .1 <= args.interval_seconds <= 60):
        raise SystemExit("invalid bounds")
    if bool(args.venue_terms_directory) != args.collect_venue_terms:
        raise SystemExit("venue collection requires an explicit receipt archive directory")
    venue_cache=None
    if args.collect_venue_terms:
        from v7_exact_arb_venue_selection import VenueTermsCache
        venue_cache=VenueTermsCache(args.venue_terms_directory)
    while True:
        try:
            selection, status = compile_selection(
                load(args.graph), load(args.universe), load(args.hotset),
                args.model_sha, args.max_markets,
                as_of_ms=time.time_ns() // 1_000_000,
            )
            if venue_cache is not None:
                now=time.time_ns()//1_000_000
                if venue_cache.due(selection,now):
                    # Publication failure stops before network work. Old terms
                    # are not granted continuity across a failed source refresh.
                    atomic(args.output,{**selection,"source_valid":False,"valid_until_ms":0,
                                        "venue_terms_refreshing":True})
                    atomic(args.status,{**status,"state":"VENUE_TERMS_REFRESHING"})
                    venue_cache.refresh(selection,now)
                    selection,status=compile_selection(load(args.graph),load(args.universe),load(args.hotset),
                        args.model_sha,args.max_markets,as_of_ms=time.time_ns()//1_000_000)
                selection=venue_cache.attach(selection,time.time_ns()//1_000_000)
                status["venue_terms_states"]={state:sum(row["state"]==state for row in selection["venue_terms"]["receipts"])
                    for state in ("OBSERVED_SUPPORTED_TERMS","UNVERIFIED")}
            atomic(args.output, selection)
            atomic(args.status, status)
            print(json.dumps(status, sort_keys=True), flush=True)
            if args.once:
                return 0
        except Exception as exc:
            failure = {
                "schema": STATUS_SCHEMA,
                **SAFETY,
                "execution_authority": False,
                "model_sha": args.model_sha,
                "state": "BLOCKED",
                "error": type(exc).__name__,
                "timestamp_ms": time.time_ns() // 1_000_000,
            }
            atomic(args.output, {**failure, "schema": SCHEMA, "source_valid": False,
                                "selection_only": True, "markets": [], "valid_until_ms": 0})
            atomic(args.status, failure)
            print(json.dumps(failure, sort_keys=True), flush=True)
            if args.once:
                return 2
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
