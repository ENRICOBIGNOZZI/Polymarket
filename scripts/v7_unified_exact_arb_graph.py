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
    return found if all(found) and all(found.values()) and len(found) == len(ids) else None


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
        # Every nonempty selector subset is expensive to index and unnecessary:
        # resolve scans only the small live universe after a dependency compile.
    return out


def resolve_market(markets: list[dict[str, Any]], selector: dict[str, Any]) -> dict[str, Any] | None:
    key = selector_key(selector); matches = []
    for row in markets:
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


def prove(raw: dict[str, Any]) -> dict[str, Any]:
    """Equality is actionable; proven inequalities are retained but disabled."""
    kind=str(raw.get("relation_type") or "CONSTANT_PAYOUT_EQUALITY")
    if kind == "CONSTANT_PAYOUT_EQUALITY": return prove_relation(raw)
    if kind not in {"PAYOFF_UPPER_BOUND","PAYOFF_LOWER_BOUND"}: raise GraphError("relation_type")
    states, legs, guarantee=raw.get("states"),raw.get("legs"),frac(raw.get("guaranteed_payout"))
    if not isinstance(states,list) or not states or not isinstance(legs,list) or not legs: raise GraphError("inequality_shape")
    totals=[]
    for i in range(len(states)):
        total=sum((frac(leg.get("coefficient",1))*frac(leg["payout_vector"][i]) for leg in legs),Fraction(0));totals.append(total)
    if (kind=="PAYOFF_UPPER_BOUND" and any(x>guarantee for x in totals)) or (kind=="PAYOFF_LOWER_BOUND" and any(x<guarantee for x in totals)):
        raise GraphError("invalid_inequality")
    body={"type":kind,"states":states,"totals":[fstr(x) for x in totals],"guarantee":fstr(guarantee)}
    return {"proof_type":"FINITE_STATE_EXACT_RATIONAL_INEQUALITY","proof_sha256":sha(body),"state_totals":body["totals"]}


def _compile_relation(raw: dict[str, Any], markets: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    proof = prove(raw)  # Fraction-based statewise equality/inequality proof.
    states, legs = raw["states"], raw["legs"]
    compiled_legs, nodes = [], []
    for leg in legs:
        outcome = str(leg.get("outcome") or "").upper()
        market = resolve_market(markets, leg.get("selector"))
        mapping = outcome_map(market) if market is not None else None
        if mapping is None or outcome not in mapping:
            raise GraphError("unresolved_claim")
        claim = node(market, mapping[outcome], outcome)
        coefficient = frac(leg.get("coefficient", 1))
        if coefficient <= 0: raise GraphError("non_positive_coefficient")
        vector = [fstr(frac(value)) for value in leg["payout_vector"]]
        compiled_legs.append({"node_id": claim["node_id"], "token_id": claim["token_id"],
                              "market_id": claim.get("market_id"), "condition_id": claim.get("condition_id"),
                              "settlement_semantic_hash": claim.get("settlement_semantic_hash"),
                              "coefficient": fstr(coefficient), "payout_vector": vector,
                              "fee_semantics": leg.get("fee_semantics", "MARKET_VERIFIED_REQUIRED"),
                              "fee_rate": fee_rate(market),
                              "fee_exponent": fee_exponent(market),
                              "minimum_order": fstr(frac(leg.get("minimum_order", 0)))})
        nodes.append(claim)
    terminal = {str(state): fstr(sum((frac(leg["coefficient"]) * frac(leg["payout_vector"][i]) for leg in compiled_legs), Fraction(0)))
                for i, state in enumerate(states)}
    guarantee = fstr(frac(raw["guaranteed_payout"]))
    relation = {"relation_id": str(raw.get("id") or raw.get("relation_id") or ""),
                "relation_family": str(raw.get("relation_family") or raw.get("discovery") or "EXPLICIT_EXACT"),
                "states": [str(value) for value in states], "legs": compiled_legs,
                "guaranteed_payout": guarantee, "terminal_payout_vector": terminal,
                "proof_type": proof["proof_type"], "proof_hash": proof["proof_sha256"],
                "settlement_semantic_dependencies": sorted({str(item["settlement_semantic_hash"])
                    for item in nodes if item.get("settlement_semantic_hash")}),
                "source_provenance": str(raw.get("discovery") or raw.get("provenance") or "EXPLICIT_VERIFIED"),
                "verification": "EXPLICIT_VERIFIED", "verified_at_ms": int(raw.get("verified_at_ms") or 0),
                "execution_semantics": raw.get("execution_semantics", "SEQUENTIAL_PARALLEL_BATCH_SHADOW"),
                "capital_transformation_semantics": raw.get("capital_transformation_semantics", "SETTLEMENT_LOCKED"),
                "capital_lock_time_ms": int(raw["capital_lock_time_ms"]) if raw.get("capital_lock_time_ms") is not None else None,
                "collateral_denomination": raw.get("collateral_denomination") or next((item.get("collateral_denomination") for item in nodes if item.get("collateral_denomination")), None),
                "transformation": raw.get("transformation") if isinstance(raw.get("transformation"), dict) else None,
                "reserve_per_unit": fstr(frac(raw.get("reserve_per_unit", 0))),
                "relation_type": str(raw.get("relation_type") or "CONSTANT_PAYOUT_EQUALITY"),
                "directions": directions(raw),
                "enabled": raw.get("enabled") is True and str(raw.get("relation_type") or "CONSTANT_PAYOUT_EQUALITY")=="CONSTANT_PAYOUT_EQUALITY", "automatic_promotion": False}
    if not relation["relation_id"]: raise GraphError("relation_id")
    relation["economic_identity"] = sha({"legs": sorted((leg["token_id"], leg["coefficient"]) for leg in compiled_legs),
                                          "terminal": terminal, "guarantee": guarantee})
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
            "states":["YES","NO"],"guaranteed_payout":1,"legs":[
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
            "polymarket_v7_combo_rfq_shadow_v1",
            "polymarket_v7_combo_collateral_return_shadow_v1",
        } and not isinstance(attestations, list):
            candidates.append({"relation_family": "LEGACY_COMPONENT", "verification": "UNVERIFIED_CANDIDATE",
                               "source_schema": schema, "reason": "finite_state_attestation_required"})
    return relations, candidates, provenance


def compile_graph(registries: list[dict[str, Any]], universe: dict[str, Any], model_sha: str,
                  component_inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not safe(universe, model_sha): raise GraphError("unsafe_universe")
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
    component_rows, component_candidates, component_provenance = component_sources(component_inputs or [], model_sha)
    sources.extend(component_rows)
    relations, nodes, rejected = [], {}, []
    for source in sources:
        try:
            relation, claims = _compile_relation(source, markets)
        except (GraphError, ValueError, KeyError, TypeError) as exc:
            rejected.append({"relation_id": str(source.get("id") or source.get("relation_id") or ""),
                             "reason": type(exc).__name__})
            continue
        if any(existing.get("node_id") == claim["node_id"] and existing != claim for existing in nodes.values() for claim in claims):
            raise GraphError("claim_identity_collision")
        for claim in claims: nodes[claim["node_id"]] = claim
        relations.append(relation)
    if len({row["relation_id"] for row in relations}) != len(relations): raise GraphError("duplicate_relation_id")
    index: dict[str, list[int]] = defaultdict(list)
    for handle, relation in enumerate(relations):
        for leg in relation["legs"]: index[leg["token_id"]].append(handle)
    unverified=[{"relation_family":"NEGRISK_TRANSFORMATION","verification":"UNVERIFIED_CANDIDATE","market_id":str(row.get("market_id") or ""),"reason":"no_verified_conversion_semantics"}
                for row in markets if row.get("neg_risk") is True or row.get("negRisk") is True]
    unverified.extend(component_candidates)
    graph = {"schema": SCHEMA, "version": 1, **SAFETY, "model_sha": model_sha,
             "nodes": sorted(nodes.values(), key=lambda value: value["node_id"]), "relations": relations,
             "dependency_index": {key: value for key, value in sorted(index.items())},
             "proof_registry": {row["proof_hash"]: row["relation_id"] for row in relations},
             "rejected_relations": rejected, "unverified_candidates": unverified,
             "component_provenance": component_provenance,
             "metadata": {"control_plane": True, "hot_path_contract": "TOKEN_HANDLE_TO_AFFECTED_RELATIONS_ONLY",
                          "actionable_relations": sum(row["enabled"] for row in relations),
                          "compiled_at_ms": time.time_ns() // 1_000_000}}
    graph["graph_generation"] = sha({key: value for key, value in graph.items() if key not in {"metadata", "graph_generation"}})
    return graph


def levels(raw: Any) -> list[tuple[Fraction, Fraction]]:
    out = []
    if not isinstance(raw, list): return out
    for value in raw[:1024]:
        try: price, size = frac(value[0]), frac(value[1])
        except (IndexError, TypeError, GraphError): continue
        if 0 < price < 1 and size > 0: out.append((price, size))
    return sorted(out)


def round_fee(value: Fraction, increment: Fraction | None, mode: str) -> Fraction:
    """Apply an explicitly declared exact fee rounding convention."""
    if increment is None or mode == "EXACT": return value
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
    """Exact full-depth sizing with depth breakpoints; never treats missing fees/depth as zero."""
    if relation.get("enabled") is not True: return {"accepted": False, "reason": "disabled_relation"}
    prepared, candidates, times = [], set(), []
    for leg in relation.get("legs") or []:
        book = books.get(str(leg.get("token_id")))
        if not isinstance(book, dict) or book.get("lineage_continuous") is not True: return {"accepted": False, "reason": "lineage_or_book_missing"}
        if book.get("depth_truncated") is True: return {"accepted": False, "reason": "truncated_depth"}
        try:
            raw_fee = book.get("fee_rate") if book.get("fee_rate") is not None else leg.get("fee_rate")
            stamp, fee, coefficient = int(book["timestamp_ms"]), frac(raw_fee), frac(leg["coefficient"])
            raw_exponent = book.get("fee_exponent") if book.get("fee_exponent") is not None else leg.get("fee_exponent")
            exponent = frac(0 if raw_exponent is None else raw_exponent)
        except (KeyError, TypeError, ValueError, GraphError): return {"accepted": False, "reason": "fee_or_timestamp_missing"}
        asks, bids = levels(book.get("asks")), levels(book.get("bids"))
        if fee < 0 or exponent < 0 or exponent.denominator != 1 or not asks: return {"accepted": False, "reason": "fee_or_depth_invalid"}
        if stamp <= 0 or now_ms - stamp > maximum_age_ms: return {"accepted": False, "reason": "stale_book"}
        cumulative = Fraction(0)
        for _, size in asks: cumulative += size; candidates.add(cumulative / coefficient)
        prepared.append((leg, asks, bids, fee, int(exponent), coefficient)); times.append(stamp)
    if not prepared: return {"accepted": False, "reason": "empty_relation"}
    if max(times) - min(times) > maximum_skew_ms: return {"accepted": False, "reason": "leg_skew"}
    guarantee, reserve, best = frac(relation["guaranteed_payout"]), frac(relation.get("reserve_per_unit", 0)), None
    minimum=max((frac(leg.get("minimum_order",0))/coefficient for leg,_,_,_,_,coefficient in prepared),default=Fraction(0))
    cap=frac(capital_limit)
    raw_touch=sum((depth[0][0]*coefficient for _,depth,_,_,_,coefficient in prepared),Fraction(0))
    fee_touch=sum(((depth[0][0] + fee_per_share(depth[0][0], fee, exponent))*coefficient
                   for _,depth,_,fee,exponent,coefficient in prepared),Fraction(0))
    distances={"distance_to_raw_arbitrage":fstr(raw_touch-guarantee),
               "distance_to_after_fee_arbitrage":fstr(fee_touch-guarantee),
               "distance_to_after_reserve_arbitrage":fstr(fee_touch+reserve-guarantee)}
    permitted = set(relation.get("directions") or ["BUY_BASKET"])
    for direction in ("BUY", "SELL"):
        required = "BUY_COMPLETE_SET" if direction == "BUY" else "SELL_COMPLETE_SET"
        alias = "BUY_BASKET" if direction == "BUY" else "SELL_INVENTORY_BASKET"
        if required not in permitted and alias not in permitted: continue
        local_candidates = set(candidates)
        if direction == "SELL":
            if inventory_limit is None: continue
            local_candidates = set()
            for _, _, bids, _, _, coefficient in prepared:
                cumulative = Fraction(0)
                for _, size in bids:
                    cumulative += size; local_candidates.add(cumulative / coefficient)
            local_candidates.add(frac(inventory_limit))
        for quantity in sorted(local_candidates):
            if quantity <= 0 or quantity < minimum: continue
            if direction == "SELL" and quantity > frac(inventory_limit): continue
            total = Fraction(0)
            for leg, asks, bids, fee, exponent, coefficient in prepared:
                book = asks if direction == "BUY" else bids
                try:
                    increment = frac(leg["fee_rounding_increment"]) if leg.get("fee_rounding_increment") is not None else None
                    value = walk(book, quantity * coefficient, fee, exponent, direction, increment,
                                 str(leg.get("fee_rounding_mode") or "EXACT"))
                except GraphError:
                    return {"accepted": False, "reason": "fee_rounding_invalid", **distances}
                if value is None: break
                total += value[0]
            else:
                capital = total + quantity * reserve if direction == "BUY" else quantity * reserve
                if capital > cap: continue
                pnl = (quantity * guarantee - total - quantity * reserve if direction == "BUY"
                       else total - quantity * guarantee - quantity * reserve)
                if best is None or pnl > best[3]: best = (direction, quantity, total, pnl, capital)
    if best is None: return {"accepted": False, "reason": "minimum_order_or_capital",**distances}
    if best[2] <= 0: return {"accepted": False, "reason": "edge_after_costs_nonpositive",**distances}
    direction, quantity, cost, pnl, capital = best
    lock = relation.get("capital_lock_time_ms")
    try: lock_value = int(lock) if lock is not None else None
    except (TypeError, ValueError): return {"accepted": False, "reason": "capital_lock_invalid", **distances}
    if lock_value is not None and lock_value <= 0: return {"accepted": False, "reason": "capital_lock_invalid", **distances}
    result = {"accepted": True, "reason": "candidate", "direction": direction, "quantity": fstr(quantity), "capital_required": fstr(capital),
              "net_locked_pnl": fstr(pnl), "capital_lock_time_ms": lock_value, **distances}
    if lock_value is not None and capital > 0:
        result["net_locked_pnl_per_capital_time"] = fstr(pnl / capital / lock_value)
    return result


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
            graph = compile_graph([load(path) for path in args.registry], load(args.universe), args.model_sha,
                                  [load(path) for path in args.component_status])
            tmp = args.output.with_suffix(args.output.suffix + ".tmp")
            tmp.write_text(json.dumps(graph, sort_keys=True, indent=2) + "\n", encoding="utf-8"); tmp.replace(args.output)
            print(json.dumps({"graph_generation": graph["graph_generation"], "nodes": len(graph["nodes"]), "relations": len(graph["relations"])}), flush=True)
            if args.once: return 0
        except GraphError as exc:
            print(f"unified exact arb graph: {exc}", flush=True)
            if args.once: return 2
        time.sleep(args.interval_seconds)

if __name__ == "__main__": raise SystemExit(main())
