#!/usr/bin/env python3
"""Immutable, zero-authority exact-arbitrage hypergraph compiler and evaluator.

This is deliberately a control/shadow component.  It has no HTTP, websocket,
credential, signing, OMS or order-submission dependency.  The existing C++
PureArb lane remains the frozen binary champion; this module consumes only
verified finite-state relations and exposes a token dependency index for a
separate event-driven runner.
"""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from v7_exact_relation_discovery import prove_relation

SCHEMA = "polymarket_v7_unified_exact_arb_graph_v1"
SAFETY = {"paper_only": True, "authenticated_execution": False,
          "real_order_submission": False, "real_capital_at_risk": False,
          "automatic_promotion": False}


class GraphError(ValueError):
    pass


def load(path: Path) -> dict[str, Any]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {}
    return value if isinstance(value, dict) else {}


def frac(value: Any) -> Fraction:
    if isinstance(value, bool): raise GraphError("boolean_rational")
    try: result = Fraction(str(value))
    except (ValueError, ZeroDivisionError): raise GraphError("invalid_rational") from None
    return result


def fstr(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe(value: dict[str, Any], model_sha: str | None = None) -> bool:
    return (all(value.get(key) is expected for key, expected in SAFETY.items())
            and (model_sha is None or value.get("model_sha") == model_sha))


def outcome_map(market: dict[str, Any]) -> dict[str, str] | None:
    ids, outcomes = market.get("clob_token_ids"), market.get("outcomes")
    if (not isinstance(ids, list) or not isinstance(outcomes, list) or len(ids) < 2
            or len(ids) != len(outcomes) or len(ids) > 32):
        return None
    found = {str(outcomes[i]).upper(): str(ids[i]) for i in range(len(ids))}
    return found if all(found) and all(found.values()) and len(found) == len(ids) and len(set(ids)) == len(ids) else None


def token_map(market: dict[str, Any]) -> dict[str, str] | None:
    found = outcome_map(market)
    if found is None or len(found) != 2:
        return None
    yes, no = found.get("YES") or found.get("UP"), found.get("NO") or found.get("DOWN")
    return {"YES": yes, "NO": no} if yes and no and yes != no else None


def selector_key(selector: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    allowed = ("market_id", "asset", "horizon", "contract_family", "settlement_semantic_hash",
               "normalized_rules_hash", "window_start_unix", "close_timestamp_unix")
    if not isinstance(selector, dict) or not selector or any(key not in allowed for key in selector):
        raise GraphError("invalid_selector")
    return tuple(sorted((key, str(value)) for key, value in selector.items()))


def market_lookup(universe: dict[str, Any]) -> dict[tuple[tuple[str, str], ...], list[dict[str, Any]]]:
    out: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = defaultdict(list)
    for row in universe.get("markets") or []:
        if not isinstance(row, dict): continue
        for key in ("market_id",):
            if row.get(key): out[((key, str(row[key])),)].append(row)
        # Exact market IDs cover automatic binary/partition legs. Other
        # selectors remain off-path and must still resolve uniquely.
    return out


def resolve_market(markets: list[dict[str, Any]], selector: dict[str, Any], lookup=None) -> dict[str, Any] | None:
    key = selector_key(selector); matches = []
    candidates = markets if lookup is None or "market_id" not in selector else lookup.get((("market_id", str(selector["market_id"])),), [])
    for row in candidates:
        if all(str(row.get(name) or "") == wanted for name, wanted in key): matches.append(row)
    return matches[0] if len(matches) == 1 else None


def node(market: dict[str, Any], token: str, outcome: str) -> dict[str, Any]:
    identity = {key: market.get(key) for key in (
        "market_id", "event_id", "condition_id", "asset", "horizon", "contract_family",
        "window_start_unix", "close_timestamp_unix", "settlement_semantic_hash",
        "normalized_rules_hash", "neg_risk_group_id", "collateral_denomination", "fee_semantic_hash")}
    identity.update({"token_id": token, "position_id": token, "outcome": outcome})
    return {"node_id": sha(identity), **identity}


def fee_rate(market: dict[str, Any]) -> str | None:
    """Carry only explicitly verified fees; unknown fees remain non-executable."""
    fee = market.get("fee_schedule")
    try:
        rate = frac(fee["rate"]) if isinstance(fee, dict) else None
    except (GraphError, KeyError, TypeError):
        return None
    if rate is not None and 0 <= rate <= 1:
        return fstr(rate)
    if market.get("fees_enabled_explicit") is True and market.get("fees_enabled") is False:
        return "0"
    return None


def fee_exponent(market: dict[str, Any]) -> str | None:
    """Only non-negative integral fee exponents have an exact rational path."""
    fee = market.get("fee_schedule")
    if isinstance(fee, dict):
        try: exponent = frac(fee.get("exponent", 1))
        except GraphError: return None
        if exponent >= 0 and exponent.denominator == 1: return fstr(exponent)
        return None
    if market.get("fees_enabled_explicit") is True and market.get("fees_enabled") is False:
        return "1"
    return None


def directions(raw: dict[str, Any]) -> list[str]:
    """Validate the economic direction set; unknown directions never default in."""
    values = raw.get("directions", ["BUY_BASKET"])
    if not isinstance(values, list) or not values:
        raise GraphError("directions_shape")
    allowed = {"BUY_BASKET", "BUY_COMPLETE_SET", "SELL_INVENTORY_BASKET", "SELL_COMPLETE_SET"}
    normalized = sorted({str(value) for value in values})
    if any(value not in allowed for value in normalized):
        raise GraphError("invalid_direction")
    return normalized


def transformation(raw: dict[str, Any]) -> dict[str, Any] | None:
    value = raw.get("transformation")
    if value is None: return None
    if not isinstance(value, dict): raise GraphError("transformation_shape")
    kind, verification = str(value.get("kind") or ""), str(value.get("verification") or "")
    if kind not in {"MERGE", "SPLIT", "NEGRISK_CONVERSION", "COMBO_COLLATERAL_RETURN", "REDEEM"}:
        raise GraphError("transformation_kind")
    if verification not in {"AUTO_VERIFIED", "EXPLICIT_VERIFIED"}:
        raise GraphError("transformation_verification")
    try:
        capacity = frac(value["capacity"]); latency = int(value["latency_ms"]); lock = int(value["capital_lock_time_ms"])
    except (KeyError, TypeError, ValueError, GraphError): raise GraphError("transformation_terms") from None
    if capacity <= 0 or latency < 0 or lock <= 0: raise GraphError("transformation_terms")
    proof_hash=str(value.get("proof_hash") or "")
    if len(proof_hash) != 64 or any(c not in "0123456789abcdef" for c in proof_hash): raise GraphError("transformation_proof")
    return {**{key:value[key] for key in ("fixed_cost","variable_cost_per_unit","expires_at_ms") if key in value},
            "id":str(value.get("id") or sha(value)), "kind":kind,"verification":verification,
            "capacity":fstr(capacity),"latency_ms":latency,"capital_lock_time_ms":lock,
            "proof_hash":proof_hash}


def prove(raw: dict[str, Any]) -> dict[str, Any]:
    """Equality is actionable; proven inequalities are retained but disabled."""
    states=raw.get("states")
    if not isinstance(states,list) or not 1<=len(states)<=64 or len(set(map(str,states)))!=len(states):
        raise GraphError("invalid_state_space")
    kind=str(raw.get("relation_type") or "CONSTANT_PAYOUT_EQUALITY")
    if kind == "CONSTANT_PAYOUT_EQUALITY": return prove_relation(raw)
    if kind == "LOGICAL_IMPLICATION":
        states, lhs, rhs = raw.get("states"), raw.get("antecedent_payout_vector"), raw.get("consequent_payout_vector")
        if (not isinstance(states,list) or not states or not isinstance(lhs,list) or not isinstance(rhs,list)
                or len(lhs)!=len(states) or len(rhs)!=len(states)):
            raise GraphError("implication_shape")
        try: left,right=[frac(value) for value in lhs],[frac(value) for value in rhs]
        except GraphError: raise GraphError("implication_rational") from None
        if any(value < 0 for value in left+right) or any(a>b for a,b in zip(left,right)):
            raise GraphError("invalid_implication")
        body={"type":kind,"states":[str(value) for value in states],"antecedent":[fstr(value) for value in left],
              "consequent":[fstr(value) for value in right]}
        return {"proof_type":"FINITE_STATE_EXACT_RATIONAL_IMPLICATION","proof_sha256":sha(body),
                "state_totals":[f"{fstr(a)}<={fstr(b)}" for a,b in zip(left,right)]}
    if kind not in {"PAYOFF_UPPER_BOUND","PAYOFF_LOWER_BOUND"}: raise GraphError("relation_type")
    states, legs, guarantee=raw.get("states"),raw.get("legs"),frac(raw.get("guaranteed_payout"))
    if not isinstance(legs,list) or not 1<=len(legs)<=32: raise GraphError("inequality_shape")
    for leg in legs:
        if not isinstance(leg.get("payout_vector"),list) or len(leg["payout_vector"])!=len(states):raise GraphError("inequality_shape")
        if frac(leg.get("coefficient",1))<=0 or any(frac(x)<0 for x in leg["payout_vector"]):raise GraphError("inequality_payout")
    totals=[]
    for i in range(len(states)):
        total=sum((frac(leg.get("coefficient",1))*frac(leg["payout_vector"][i]) for leg in legs),Fraction(0));totals.append(total)
    if (kind=="PAYOFF_UPPER_BOUND" and any(x>guarantee for x in totals)) or (kind=="PAYOFF_LOWER_BOUND" and any(x<guarantee for x in totals)):
        raise GraphError("invalid_inequality")
    body={"type":kind,"states":states,"totals":[fstr(x) for x in totals],"guarantee":fstr(guarantee)}
    return {"proof_type":"FINITE_STATE_EXACT_RATIONAL_INEQUALITY","proof_sha256":sha(body),"state_totals":body["totals"]}


def _compile_relation(raw: dict[str, Any], markets: list[dict[str, Any]], lookup=None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    proof = prove(raw)  # Fraction-based statewise equality/inequality proof.
    states, legs = raw["states"], raw["legs"]
    compiled_legs, nodes = [], []
    negrisk_conditions = set()
    for leg in legs:
        outcome = str(leg.get("outcome") or "").upper()
        market = resolve_market(markets, leg.get("selector"), lookup)
        mapping = outcome_map(market) if market is not None else None
        if mapping is not None and outcome in {"YES", "NO"}:
            mapping = token_map(market) or mapping
        if mapping is None or outcome not in mapping:
            raise GraphError("unresolved_claim")
        if market.get("neg_risk") is True or market.get("negRisk") is True:
            negrisk_conditions.add(str(market.get("condition_id") or ""))
        claim = node(market, mapping[outcome], outcome)
        coefficient = frac(leg.get("coefficient", 1))
        if coefficient <= 0: raise GraphError("non_positive_coefficient")
        vector = [fstr(frac(value)) for value in leg["payout_vector"]]
        compiled_legs.append({"node_id": claim["node_id"], "token_id": claim["token_id"],
                              "market_id": claim.get("market_id"), "condition_id": claim.get("condition_id"),
                              "settlement_semantic_hash": claim.get("settlement_semantic_hash"),
                              "outcome": outcome,
                              "coefficient": fstr(coefficient), "payout_vector": vector,
                              "fee_semantics": leg.get("fee_semantics", "MARKET_VERIFIED_REQUIRED"),
                              "fee_rate": fee_rate(market),
                              "fee_exponent": fee_exponent(market),
                              "fee_rounding_mode": (market.get("fee_schedule") or {}).get("rounding_mode", "VENUE_5DP"),
                              "fee_rounding_increment": (market.get("fee_schedule") or {}).get("rounding_increment", "0.00001"),
                              "tick_size": market.get("tick_size"),
                              "minimum_order": fstr(frac(leg.get("minimum_order", market.get("minimum_order_size") or 0)))})
        nodes.append(claim)
    terminal = {str(state): fstr(sum((frac(leg["coefficient"]) * frac(leg["payout_vector"][i]) for leg in compiled_legs), Fraction(0)))
                for i, state in enumerate(states)}
    guarantee = fstr(frac(raw.get("guaranteed_payout", 0)))
    relation = {"relation_id": str(raw.get("id") or raw.get("relation_id") or ""),
                "relation_family": str(raw.get("relation_family") or raw.get("discovery") or "EXPLICIT_EXACT"),
                "states": [str(value) for value in states], "legs": compiled_legs,
                "guaranteed_payout": guarantee, "terminal_payout_vector": terminal,
                "proof_type": proof["proof_type"], "proof_hash": proof["proof_sha256"],
                "settlement_semantic_dependencies": sorted({str(item["settlement_semantic_hash"])
                    for item in nodes if item.get("settlement_semantic_hash")}),
                "source_provenance": str(raw.get("discovery") or raw.get("provenance") or "EXPLICIT_VERIFIED"),
                "verification": "AUTO_VERIFIED" if str(raw.get("discovery", "")).startswith("AUTO") else "EXPLICIT_VERIFIED", "verified_at_ms": int(raw.get("verified_at_ms") or 0),
                "execution_semantics": raw.get("execution_semantics", "SEQUENTIAL_PARALLEL_BATCH_SHADOW"),
                "capital_transformation_semantics": raw.get("capital_transformation_semantics", "SETTLEMENT_LOCKED"),
                "capital_lock_time_ms": int(raw["capital_lock_time_ms"]) if raw.get("capital_lock_time_ms") is not None else None,
                "settlement_close_ms": min((int(m["close_timestamp_unix"])*1000 for m in nodes
                    if int(m.get("close_timestamp_unix") or 0)>0), default=0),
                "collateral_denomination": raw.get("collateral_denomination") or next((item.get("collateral_denomination") for item in nodes if item.get("collateral_denomination")), None),
                "transformation": transformation(raw),
                "reserve_per_unit": fstr(frac(raw.get("reserve_per_unit", 0))),
                "relation_type": str(raw.get("relation_type") or "CONSTANT_PAYOUT_EQUALITY"),
                "directions": directions(raw),
                "enabled": raw.get("enabled") is True and str(raw.get("relation_type") or "CONSTANT_PAYOUT_EQUALITY")=="CONSTANT_PAYOUT_EQUALITY", "automatic_promotion": False}
    conditions={leg["condition_id"] for leg in compiled_legs}
    if (relation["relation_family"]=="NEGRISK_COMPLETE_SET"
            or (negrisk_conditions and len(conditions)>1)
            or (relation["transformation"] or {}).get("kind")=="NEGRISK_CONVERSION"):
        # Registry and component booleans cannot bypass the same missing
        # independent semantics. Preserve the mathematical proof as research
        # material, but do not enable a cross-condition NegRisk equality.
        # Same-condition binary complete sets retain their separate semantics.
        relation.update(enabled=False,verification="UNVERIFIED_CANDIDATE",
            semantic_rejection_reason="independent_negrisk_exhaustiveness_or_conversion_proof_required")
    if not relation["relation_id"]: raise GraphError("relation_id")
    if len({leg["token_id"] for leg in compiled_legs}) != len(compiled_legs):
        raise GraphError("duplicate_token_claim")
    unit = frac(guarantee)
    if unit <= 0:
        if relation["relation_type"] == "CONSTANT_PAYOUT_EQUALITY": raise GraphError("non_positive_guarantee")
        unit = Fraction(1)
    relation["economic_identity"] = sha({
        "claims": sorted((leg["node_id"], fstr(frac(leg["coefficient"])/unit)) for leg in compiled_legs),
        "terminal": {"constant":"1"} if relation["relation_type"]=="CONSTANT_PAYOUT_EQUALITY" else terminal,
        "collateral":relation["collateral_denomination"], "relation_type":relation["relation_type"]})
    return relation, nodes


def automatic_binary_relations(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate only explicitly machine-attested same-condition partitions."""
    result=[]
    for market in markets:
        mapping=token_map(market)
        if (mapping is None or market.get("binary_partition_verified") is not True
                or not str(market.get("condition_id") or "")
                or len(str(market.get("settlement_semantic_hash") or "")) != 64): continue
        selector={"market_id":str(market["market_id"])}
        result.append({"id":"binary:"+str(market["market_id"]),"enabled":True,
            "relation_family":"SAME_MARKET_BINARY_COMPLETE_SET","discovery":"AUTO_VERIFIED_BINARY_PARTITION",
            "directions":["BUY_COMPLETE_SET","SELL_COMPLETE_SET"],
            "reserve_per_unit":"0.0005", "states":["YES","NO"],"guaranteed_payout":1,"legs":[
            {"selector":selector,"outcome":"YES","coefficient":1,"payout_vector":[1,0]},
            {"selector":selector,"outcome":"NO","coefficient":1,"payout_vector":[0,1]}]})
    return result


def automatic_partition_relations(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compile an N-way hyperedge only from a machine-attested payout matrix."""
    result = []
    for market in markets:
        mapping = outcome_map(market)
        states = market.get("partition_states")
        vectors = market.get("partition_payout_vectors")
        if (mapping is None or len(mapping) <= 2 or market.get("partition_verified") is not True
                or not str(market.get("condition_id") or "")
                or len(str(market.get("settlement_semantic_hash") or "")) != 64
                or not isinstance(states, list) or not states or not isinstance(vectors, dict)):
            continue
        legs = []
        try:
            for outcome in sorted(mapping):
                vector = vectors.get(outcome)
                if not isinstance(vector, list) or len(vector) != len(states): raise GraphError("partition_vector")
                legs.append({"selector":{"market_id":str(market["market_id"])}, "outcome":outcome,
                             "coefficient":1, "payout_vector":vector})
            raw = {"id":"partition:"+str(market["market_id"]), "enabled":True,
                   "relation_family":"N_WAY_COMPLETE_PARTITION", "discovery":"AUTO_VERIFIED_PARTITION",
                   "directions":["BUY_BASKET"], "states":states, "guaranteed_payout":1, "legs":legs}
            prove_relation(raw)
        except (GraphError, ValueError, KeyError, TypeError):
            continue
        result.append(raw)
    return result


def automatic_negrisk_relations(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """No one-hot state space may be manufactured from metadata booleans.

    The independent block verifier can observe all current contract members,
    but the adapter enforces AT MOST one winner, not an exactly-one proof for
    an unresolved event. It also allows the oracle to append questions. Until
    an independent semantic verifier supports exhaustiveness, these candidates
    cannot enter through this automatic discovery path. Explicit finite-state
    research registries retain their mathematical proof, but cannot bypass
    the same semantic admission gate in _compile_relation.
    """
    return []


def component_sources(inputs: list[dict[str, Any]], model_sha: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Adapt legacy shadows into the one graph control-plane without trusting them.

    A component may contribute a relation only through an explicit, safety-bound
    finite-state attestation.  Ordinary shadow rows are retained as provenance
    and UNVERIFIED_CANDIDATE observations; this makes missing semantics visible
    instead of inventing a conversion from a title, price, or RFQ quote.
    """
    relations, candidates, provenance = [], [], []
    for value in inputs:
        schema = str(value.get("schema") or "UNKNOWN")
        if schema == "polymarket_v7_negrisk_block_attestation_v1":
            from v7_exact_arb_negrisk_attestation import validate_receipt
            try:
                observed=validate_receipt(value)
            except (ValueError,TypeError,KeyError):
                candidates.append({"relation_family":"NEGRISK_COMPLETE_SET","verification":"UNVERIFIED_CANDIDATE",
                                   "source_schema":schema,"reason":"invalid_negrisk_block_attestation"})
                provenance.append({"schema":schema,"safe":False})
                continue
            # Historical model-independent facts are allowed only as disabled
            # provenance, never as a live lease or a finite-state equality.
            provenance.append({"schema":schema,"safe":True,"proof_hash":value["proof_hash"],
                               "block":observed["block"],"scope":"HISTORICAL_BLOCK_OBSERVATION_ONLY"})
            candidates.append({"relation_family":"NEGRISK_COMPLETE_SET","verification":"UNVERIFIED_CANDIDATE",
                "event_id":observed["event_id"],"membership_hash":observed["membership_hash"],
                "reason":"independent_terminal_exhaustiveness_unproven","source_schema":schema,
                "attestation_reasons":observed["reasons"],"proof_hash":value["proof_hash"]})
            continue
        safe_input = (value.get("model_sha") == model_sha and value.get("paper_only") is True
                      and value.get("authenticated_execution") is False
                      and value.get("real_order_submission") is False)
        provenance.append({"schema": schema, "safe": safe_input,
                           "timestamp_ms": value.get("timestamp_ms") or value.get("generated_at_ms")})
        if not safe_input:
            candidates.append({"relation_family": "COMPONENT_INPUT", "verification": "UNVERIFIED_CANDIDATE",
                               "source_schema": schema, "reason": "unsafe_or_stale_component"})
            continue
        attestations = value.get("exact_relation_attestations")
        if isinstance(attestations, list):
            for attestation in attestations:
                if not isinstance(attestation, dict) or attestation.get("verified") is not True:
                    candidates.append({"relation_family": "COMPONENT_ATTESTATION", "verification": "UNVERIFIED_CANDIDATE",
                                       "source_schema": schema, "reason": "attestation_not_verified"})
                    continue
                relation = attestation.get("relation")
                if not isinstance(relation, dict):
                    candidates.append({"relation_family": "COMPONENT_ATTESTATION", "verification": "UNVERIFIED_CANDIDATE",
                                       "source_schema": schema, "reason": "attestation_relation_missing"})
                    continue
                relations.append({**relation, "enabled": relation.get("enabled") is True,
                                  "provenance": "COMPONENT_ATTESTED:" + schema})
        # Legacy component schemas intentionally have evidence/quote output,
        # rather than a finite-state relation. They are first-class graph input
        # provenance but cannot become economic edges without an attestation.
        if schema in {
            "polymarket_v7_cross_market_exact_arb_status_v1",
            "polymarket_v7_complete_set_merge_shadow_status_v1",
            "polymarket_v7_two_sided_complete_set_shadow_status_v2",
            "polymarket_v7_combo_market_source_v1",
            "polymarket_v7_combo_rfq_gateway_status_v1",
            "polymarket_v7_combo_rfq_shadow_v1",
            "polymarket_v7_combo_collateral_return_shadow_v1",
        } and not isinstance(attestations, list):
            candidates.append({"relation_family": "LEGACY_COMPONENT", "verification": "UNVERIFIED_CANDIDATE",
                               "source_schema": schema, "reason": "finite_state_attestation_required"})
    return relations, candidates, provenance


def compile_graph(registries: list[dict[str, Any]], universe: dict[str, Any], model_sha: str,
                  component_inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not safe(universe, model_sha): raise GraphError("unsafe_universe")
    if universe.get("source_valid") is False: raise GraphError("invalid_universe_source")
    sources: list[dict[str, Any]] = []
    for registry in registries:
        # Checked-in registries have no runtime SHA; generated registries must
        # match it when they carry one.
        if (not safe(registry) or registry.get("schema") != "polymarket_v7_exact_arb_relation_registry_v1"
                or (registry.get("model_sha") not in (None, model_sha))):
            raise GraphError("unsafe_registry")
        rows = registry.get("relations")
        if not isinstance(rows, list): raise GraphError("relations_shape")
        sources.extend(row for row in rows if isinstance(row, dict) and row.get("enabled") is True)
    markets = [row for row in universe.get("markets") or [] if isinstance(row, dict)
               and row.get("active") is True and row.get("closed") is not True]
    sources.extend(automatic_binary_relations(markets))
    sources.extend(automatic_partition_relations(markets))
    sources.extend(automatic_negrisk_relations(markets))
    component_rows, component_candidates, component_provenance = component_sources(component_inputs or [], model_sha)
    sources.extend(component_rows)
    relations, nodes, rejected = [], {}, []
    lookup = market_lookup({"markets": markets})
    for source in sources:
        try:
            relation, claims = _compile_relation(source, markets, lookup)
        except (GraphError, ValueError, KeyError, TypeError) as exc:
            rejected.append({"relation_id": str(source.get("id") or source.get("relation_id") or ""),
                             "reason": str(exc) or type(exc).__name__})
            continue
        for claim in claims:
            existing = nodes.get(claim["node_id"])
            if existing is not None and existing != claim: raise GraphError("claim_identity_collision")
            nodes[claim["node_id"]] = claim
        relations.append(relation)
    if len({row["relation_id"] for row in relations}) != len(relations): raise GraphError("duplicate_relation_id")
    index: dict[str, list[int]] = defaultdict(list)
    for handle, relation in enumerate(relations):
        for token in sorted({leg["token_id"] for leg in relation["legs"]}): index[token].append(handle)
    unverified=[{"relation_family":"NEGRISK_TRANSFORMATION","verification":"UNVERIFIED_CANDIDATE","market_id":str(row.get("market_id") or ""),"reason":"no_verified_conversion_semantics"}
                for row in markets if row.get("neg_risk") is True or row.get("negRisk") is True]
    unverified.extend(component_candidates)
    unverified.extend({"relation_family":relation["relation_family"],"relation_id":relation["relation_id"],
        "verification":"UNVERIFIED_CANDIDATE","reason":relation["semantic_rejection_reason"]}
        for relation in relations if relation.get("semantic_rejection_reason"))
    unverified.extend({"relation_family":"NEGRISK_COMPLETE_SET","verification":"UNVERIFIED_CANDIDATE",
        "market_id":str(row.get("market_id") or ""),"event_id":str(row.get("event_id") or ""),
        "reason":"metadata_flag_is_not_independent_terminal_exhaustiveness_proof"}
        for row in markets if row.get("neg_risk") is True or row.get("negRisk") is True)
    for market in markets:
        mapping=outcome_map(market)
        if mapping and not (market.get("binary_partition_verified") is True if len(mapping)==2 else market.get("partition_verified") is True):
            unverified.append({"relation_family":"SAME_MARKET_BINARY_COMPLETE_SET" if len(mapping)==2 else "N_WAY_COMPLETE_PARTITION",
                "verification":"UNVERIFIED_CANDIDATE","market_id":market.get("market_id"),
                "reason":"partition_attestation_missing","source_metadata":node(market,"",""),"proof_hash":None})
    graph = {"schema": SCHEMA, "version": 1, **SAFETY, "model_sha": model_sha,
             "source_universe_valid": universe.get("source_valid"),
             "source_universe_timestamp_ms": universe.get("timestamp_ms"),
             "source_universe_membership_sha256": universe.get("membership_sha256"),
             "nodes": sorted(nodes.values(), key=lambda value: value["node_id"]), "relations": relations,
             "dependency_index": {key: value for key, value in sorted(index.items())},
             "proof_registry": {row["proof_hash"]: row["relation_id"] for row in relations},
             "rejected_relations": rejected, "unverified_candidates": unverified,
             "component_provenance": component_provenance,
             "metadata": {"control_plane": True, "hot_path_contract": "TOKEN_HANDLE_TO_AFFECTED_RELATIONS_ONLY",
                          "actionable_relations": sum(row["enabled"] for row in relations),
                          "compiled_at_ms": time.time_ns() // 1_000_000}}
    graph["transformation_registry"] = {r["transformation"]["id"]: r["transformation"] for r in relations if r.get("transformation")}
    settlement_index = defaultdict(list)
    for i, relation in enumerate(relations):
        for key in relation["settlement_semantic_dependencies"]: settlement_index[key].append(i)
    graph["settlement_dependency_index"] = dict(sorted(settlement_index.items()))
    graph["resource_dependency_index"] = {"inventory:"+token: handles for token,handles in index.items()}
    graph["graph_generation"] = generation_hash(graph)
    return graph


def generation_hash(graph: dict[str, Any]) -> str:
    return sha({key: value for key, value in graph.items() if key not in {"metadata", "graph_generation"}})


def validate_graph(graph: dict[str, Any], model_sha: str) -> None:
    if graph.get("schema") != SCHEMA or not safe(graph, model_sha): raise GraphError("graph_identity")
    if graph.get("graph_generation") != generation_hash(graph): raise GraphError("graph_digest")
    expected: dict[str, list[int]] = defaultdict(list)
    for i, relation in enumerate(graph["relations"]):
        for token in sorted({leg["token_id"] for leg in relation["legs"]}): expected[token].append(i)
    if dict(expected) != graph.get("dependency_index"): raise GraphError("graph_dependency_index")


def levels(raw: Any, descending: bool = False) -> list[tuple[Fraction, Fraction]]:
    out = []
    if not isinstance(raw, list) or len(raw) > 1024: return out
    seen = set()
    for value in raw:
        try: price, size = frac(value[0]), frac(value[1])
        except (IndexError, TypeError, GraphError): return []
        if not 0 < price < 1 or size <= 0 or price in seen: return []
        seen.add(price); out.append((price, size))
    return sorted(out, reverse=descending)


def round_fee(value: Fraction, increment: Fraction | None, mode: str) -> Fraction:
    """Apply an explicitly declared exact fee rounding convention."""
    if increment is None or mode == "EXACT": return value
    if mode == "VENUE_5DP":
        if increment != Fraction(1, 100000): raise GraphError("fee_rounding")
        if value < increment: return Fraction(0)
        units = value / increment + Fraction(1, 2)
        return increment * (units.numerator // units.denominator)
    if increment <= 0 or mode not in {"CEILING", "FLOOR"}: raise GraphError("fee_rounding")
    units = value / increment
    whole = units.numerator // units.denominator
    if mode == "CEILING" and whole * units.denominator != units.numerator: whole += 1
    return increment * whole


def fee_per_share(price: Fraction, rate: Fraction, exponent: int) -> Fraction:
    return rate * (price * (1 - price)) ** exponent


def walk(book: list[tuple[Fraction, Fraction]], quantity: Fraction, fee: Fraction, exponent: int,
         side: str, rounding_increment: Fraction | None = None,
         rounding_mode: str = "EXACT") -> tuple[Fraction, Fraction] | None:
    """Return raw and fee-adjusted cashflow. Buys are positive costs; sells proceeds."""
    cost, remaining = Fraction(0), quantity
    for price, depth in book:
        take = min(depth, remaining)
        raw = take * price
        fee_paid = round_fee(take * fee_per_share(price, fee, exponent), rounding_increment, rounding_mode)
        cost += raw + fee_paid if side == "BUY" else raw - fee_paid
        remaining -= take
        if remaining == 0: return cost, quantity
    return None


def evaluate(relation: dict[str, Any], books: dict[str, dict[str, Any]], now_ms: int,
             maximum_age_ms: int = 1000, maximum_skew_ms: int = 50,
             capital_limit: Fraction | str = "1000000000",
             inventory_limit: Fraction | str | None = None) -> dict[str, Any]:
    """Joint depth-breakpoint sweep, with exact rounded fees and microshare caps.

    Fee rounding uses the same synchronized execution slices as the native
    champion. Stop at the first nonpositive marginal slice. Unknown inventory
    never permits a SELL. Capital-limited partial slices are searched exactly.
    """
    if relation.get("enabled") is not True: return {"accepted":False,"reason":"disabled_relation"}
    if relation.get("settlement_close_ms",0) and now_ms >= relation["settlement_close_ms"]:
        return {"accepted":False,"reason":"market_closed"}
    prepared=[]; times=[]; tokens=set(); truncated=False
    for leg in relation.get("legs",[]):
        token=str(leg.get("token_id"))
        if token in tokens: return {"accepted":False,"reason":"duplicate_token_claim"}
        tokens.add(token); book=books.get(token)
        if not isinstance(book,dict) or book.get("lineage_continuous") is not True:
            return {"accepted":False,"reason":"lineage_or_book_missing"}
        if book.get("depth_truncated") is not False: truncated=True
        try:
            rate=frac(book["fee_rate"] if book.get("fee_rate") is not None else leg.get("fee_rate"))
            raw_exp=book.get("fee_exponent",leg.get("fee_exponent"))
            if raw_exp is None and rate!=0: raise GraphError("fee_exponent_missing")
            exponent=frac(1 if raw_exp is None else raw_exp)
            stamp=int(book["timestamp_ms"]); coefficient=frac(leg["coefficient"])
            if (leg.get("fee_rate") is not None and book.get("fee_rate") is not None
                and frac(leg["fee_rate"])!=rate): return {"accepted":False,"reason":"fee_changed"}
            if leg.get("tick_size") and book.get("tick_size") and frac(leg["tick_size"])!=frac(book["tick_size"]):
                return {"accepted":False,"reason":"tick_changed"}
        except (KeyError,ValueError,TypeError): return {"accepted":False,"reason":"fee_or_timestamp_missing"}
        if not 0<=rate<=1 or not 0<=exponent<=16 or exponent.denominator!=1 or coefficient<=0:
            return {"accepted":False,"reason":"fee_or_depth_invalid"}
        if stamp<=0 or not 0<=now_ms-stamp<=maximum_age_ms: return {"accepted":False,"reason":"stale_book"}
        try:
            increment=frac(leg["fee_rounding_increment"]) if leg.get("fee_rounding_increment") is not None else None
            mode=str(leg.get("fee_rounding_mode") or "EXACT")
            round_fee(Fraction(1),increment,mode)
        except GraphError: return {"accepted":False,"reason":"fee_rounding_invalid"}
        prepared.append((leg,book,coefficient,rate,int(exponent),increment,mode));times.append(stamp)
    if not prepared: return {"accepted":False,"reason":"empty_relation"}
    if max(times)-min(times)>maximum_skew_ms: return {"accepted":False,"reason":"leg_skew"}
    guarantee,reserve,cap=frac(relation["guaranteed_payout"]),frac(relation.get("reserve_per_unit",0)),frac(capital_limit)
    if guarantee<=0 or reserve<0 or cap<0: return {"accepted":False,"reason":"invalid_economics"}
    minimum=max(frac(leg.get("minimum_order",0))/c for leg,_,c,_,_,_,_ in prepared)
    # Relation quantum ensures every coefficient*quantity is an integer microshare.
    denominator=1
    for _,_,c,_,_,_,_ in prepared:
        denominator=denominator*c.denominator//math.gcd(denominator,c.denominator)
    quantum=Fraction(denominator,1000000)
    transform=relation.get("transformation")
    try: transform_cap=frac(transform["capacity"]) if transform is not None else None
    except (KeyError,ValueError,TypeError): return {"accepted":False,"reason":"transformation_invalid"}
    fixed_transform=variable_transform=Fraction(0)
    if transform is not None:
        try:
            fixed_transform=frac(transform["fixed_cost"]);variable_transform=frac(transform["variable_cost_per_unit"])
            if min(fixed_transform,variable_transform)<0 or int(transform["expires_at_ms"])<now_ms:
                raise GraphError("transformation_cost_or_expiry")
        except (KeyError,ValueError,TypeError):return {"accepted":False,"reason":"transformation_terms_missing"}
    candidates=[]; failures=[]; all_distances={}
    permitted=set(relation.get("directions") or ["BUY_BASKET"])
    for direction in ("BUY","SELL"):
        if not permitted.intersection({"BUY_BASKET","BUY_COMPLETE_SET"} if direction=="BUY"
                                      else {"SELL_INVENTORY_BASKET","SELL_COMPLETE_SET"}): continue
        depths=[levels(b.get("asks" if direction=="BUY" else "bids"),direction=="SELL") for _,b,_,_,_,_,_ in prepared]
        if not all(depths): failures.append("fee_or_depth_invalid");continue
        raw_unit=fee_unit=Fraction(0)
        for (_,_,c,rate,exponent,inc,mode),depth in zip(prepared,depths):
            price=depth[0][0];raw_unit+=c*price;fee_unit+=round_fee(c*fee_per_share(price,rate,exponent),inc,mode)
        distance=raw_unit-guarantee if direction=="BUY" else guarantee-raw_unit
        diagnostics={"distance_to_raw_arbitrage":fstr(distance),"distance_to_after_fee_arbitrage":fstr(distance+fee_unit),
                     "distance_to_after_reserve_arbitrage":fstr(distance+fee_unit+reserve)}
        all_distances[direction]=diagnostics
        if truncated: failures.append("truncated_depth");continue
        if direction=="SELL" and inventory_limit is None: failures.append("inventory_unavailable");continue
        capacity=min(sum((size for _,size in depth),Fraction(0))/p[2] for depth,p in zip(depths,prepared))
        if transform_cap is not None: capacity=min(capacity,transform_cap)
        if direction=="SELL":capacity=min(capacity,frac(inventory_limit))
        if capacity<=0:failures.append("inventory_unavailable" if direction=="SELL" else "transformation_capacity");continue
        index=[0]*len(prepared);remaining=[depth[0][1] for depth in depths];used=[0]*len(prepared)
        quantity=notional=fees=Fraction(0);capital=fixed_transform;pnl=-fixed_transform
        stop="depth_insufficient"
        while quantity<capacity:
            # Discard non-executable level dust conservatively. Otherwise a
            # rational coefficient can strand the sweep at its first breakpoint.
            for i,p in enumerate(prepared):
                while index[i]<len(depths[i]) and remaining[i]<p[2]*quantum:
                    index[i]+=1
                    if index[i]<len(depths[i]):remaining[i]=depths[i][index[i]][1]
            if any(index[i]>=len(depths[i]) for i in range(len(prepared))):break
            step=min([capacity-quantity]+[remaining[i]/p[2] for i,p in enumerate(prepared)])
            step=(step//quantum)*quantum
            if step<=0:break
            def costs(q):
                raw=fee=Fraction(0)
                for i,(_,_,c,rate,exp,inc,mode) in enumerate(prepared):
                    price=depths[i][index[i]][0]
                    raw+=q*c*price
                    fee+=round_fee(q*c*fee_per_share(price,rate,exp),inc,mode)
                required=(raw+fee if direction=="BUY" else Fraction(0))+q*(reserve+variable_transform)
                edge=(q*guarantee-raw if direction=="BUY" else raw-q*guarantee)-fee-q*(reserve+variable_transform)
                return raw,fee,required,edge
            if costs(step)[2]+capital>cap:
                low,high=0,int(step//quantum)
                while low<high:
                    mid=(low+high+1)//2
                    if costs(mid*quantum)[2]+capital<=cap:low=mid
                    else:high=mid-1
                step=low*quantum
                if step<=0:stop="capital_limit";break
            raw,fee,required,edge=costs(step)
            if edge<=0:stop="edge_after_costs_nonpositive";break
            quantity+=step;notional+=raw;fees+=fee;capital+=required;pnl+=edge
            for i,p in enumerate(prepared):
                remaining[i]-=step*p[2];used[i]=index[i]+1
                if remaining[i]==0:
                    index[i]+=1
                    if index[i]<len(depths[i]):remaining[i]=depths[i][index[i]][1]
            if any(index[i]>=len(depths[i]) for i in range(len(prepared))):break
        if quantity<=0:failures.append(stop);continue
        if quantity<minimum:
            failures.append("transformation_capacity" if transform_cap is not None and transform_cap<minimum else "minimum_order");continue
        if pnl<=0:failures.append("transformation_cost");continue
        raw_pnl=quantity*guarantee-notional if direction=="BUY" else notional-quantity*guarantee
        lock=relation.get("capital_lock_time_ms")
        if lock is None and transform is not None:lock=transform.get("capital_lock_time_ms")
        if lock is not None and int(lock)<=0:failures.append("capital_lock_invalid");continue
        result={"accepted":True,"reason":"candidate","direction":direction,"quantity":fstr(quantity),
                "capital_required":fstr(capital),"net_locked_pnl":fstr(pnl),"capital_lock_time_ms":lock,
                "gross_pnl":fstr(raw_pnl),"fee_drag":fstr(fees),"reserve_drag":fstr(quantity*reserve),
                "transformation_drag":fstr(fixed_transform+quantity*variable_transform),
                "gross_edge":fstr(raw_pnl/quantity),"net_edge":fstr(pnl/quantity),
                "levels_consumed_per_leg":used,**diagnostics}
        if capital>0:
            result["pnl_per_capital"]=fstr(pnl/capital)
            if lock is not None:
                result["net_locked_pnl_per_capital_time"]=fstr(pnl/capital/int(lock))
                result["pnl_per_capital_second"]=fstr(pnl/capital*1000/int(lock))
        candidates.append(result)
    if candidates:return max(candidates,key=lambda r:frac(r["net_locked_pnl"]))
    reason=failures[0] if failures else "fee_or_depth_invalid"
    return {"accepted":False,"reason":reason,**next(iter(all_distances.values()),{})}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", type=Path, required=True); ap.add_argument("--registry", type=Path, action="append", required=True)
    ap.add_argument("--model-sha", required=True); ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--component-status", type=Path, action="append", default=[])
    ap.add_argument("--interval-seconds", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if len(args.model_sha) != 40: raise SystemExit("invalid model sha")
    if not .1 <= args.interval_seconds <= 60: raise SystemExit("invalid interval")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            from v7_exact_arb_source_health import universe_lease
            universe = load(args.universe)
            universe_lease(universe, time.time_ns() // 1_000_000)
            graph = compile_graph([load(path) for path in args.registry], universe, args.model_sha,
                                  [load(path) for path in args.component_status])
            tmp = args.output.with_suffix(args.output.suffix + ".tmp")
            tmp.write_text(json.dumps(graph, sort_keys=True, indent=2) + "\n", encoding="utf-8"); tmp.replace(args.output)
            print(json.dumps({"graph_generation": graph["graph_generation"], "nodes": len(graph["nodes"]), "relations": len(graph["relations"])}), flush=True)
            if args.once: return 0
        except (GraphError, ValueError, OSError, TypeError) as exc:
            # Atomically invalidate, rather than retaining a formerly valid graph.
            invalid = {"schema": "polymarket_v7_invalid_exact_arb_graph_v1", **SAFETY,
                       "model_sha": args.model_sha, "state": "BLOCKED_SOURCE_INVALID",
                       "error": str(exc), "timestamp_ms": time.time_ns() // 1_000_000}
            tmp = args.output.with_suffix(args.output.suffix + ".tmp")
            tmp.write_text(json.dumps(invalid, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(args.output)
            print(f"unified exact arb graph: {exc}", flush=True)
            if args.once: return 2
        time.sleep(args.interval_seconds)

if __name__ == "__main__": raise SystemExit(main())
