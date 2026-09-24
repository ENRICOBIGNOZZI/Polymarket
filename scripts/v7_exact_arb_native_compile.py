#!/usr/bin/env python3
"""Off-path compiler: validated graph JSON -> immutable bounded C++ arrays.

Unsupported families/fee terms are explicitly excluded, not approximated.
The generated data has no loader, file access or allocation on the hot path.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from fractions import Fraction
from pathlib import Path
from v7_unified_exact_arb_graph import SAFETY, GraphError, frac, load, prove, validate_graph


def micro(value):
    scaled=frac(value)*1000000
    if scaled.denominator != 1 or not 0 <= scaled < 2**63: raise GraphError("native_precision_or_overflow")
    return int(scaled)


def _lower_relations(nodes, source_relations, max_relations=65536):
    """Shared off-path lowering for static headers and bounded runtime bundles."""
    tokens={node["token_id"]:i for i,node in enumerate(nodes)}
    if len(tokens)!=len(nodes): raise GraphError("native_token_collision")
    claims={node["token_id"]:node["node_id"] for node in nodes}
    relations=[]; excluded=[]; dependencies=[[] for _ in nodes]
    for r in source_relations:
        try:
            if not r["enabled"] or r["relation_type"]!="CONSTANT_PAYOUT_EQUALITY": raise GraphError("native_nonactionable")
            if not 1<=len(r["legs"])<=16: raise GraphError("native_cardinality")
            if r.get("transformation"): raise GraphError("native_transform_stage_requires_runtime_resource_evidence")
            # A matching graph checksum is integrity, not a mathematical proof.
            # Recompute the statewise rational theorem before emitting native
            # operands, even if someone recomputed a digest over corrupt data.
            prove(r)
            payout,reserve=micro(r["guaranteed_payout"]),micro(r["reserve_per_unit"])
            if payout <= 0: raise GraphError("native_non_positive_guarantee")
            legs=[]
            for leg in r["legs"]:
                if claims.get(leg["token_id"]) != leg["node_id"]: raise GraphError("native_claim_identity")
                coefficient=frac(leg["coefficient"])
                if not 0<coefficient.numerator<2**63 or not 0<coefficient.denominator<2**63: raise GraphError("native_coefficient")
                rate,exponent=frac(leg["fee_rate"]),frac(leg["fee_exponent"])
                if (not 0<=rate<=1 or not 0<=exponent<=2 or exponent.denominator!=1
                    or (rate*1000000000).denominator!=1): raise GraphError("native_fee")
                if (leg.get("fee_rounding_mode")!="VENUE_5DP" or frac(leg.get("fee_rounding_increment"))!=Fraction(1,100000)):
                    raise GraphError("native_fee_rounding_unsupported")
                legs.append((tokens[leg["token_id"]],coefficient,micro(leg["minimum_order"]),rate,exponent))
            if len({token for token,_,_,_,_ in legs})!=len(legs): raise GraphError("native_duplicate_claim")
            directions=[d for d in r["directions"] if d in {
                "BUY_BASKET","BUY_COMPLETE_SET","SELL_INVENTORY_BASKET","SELL_COMPLETE_SET"}]
            if not directions or len(set(directions))!=len(directions): raise GraphError("native_directions")
            # Preflight the whole relation: never publish BUY while rejecting
            # SELL halfway through the same immutable compilation unit.
            if len(relations)+len(directions)>max_relations: raise GraphError("native_graph_capacity")
            if any(len(dependencies[t])+len(directions)>64 for t,_,_,_,_ in legs):
                raise GraphError("native_dependency_capacity")
            for direction in directions:
                handle=len(relations)
                relations.append((handle,r,payout,reserve,legs,direction.startswith("SELL")))
                for t,_,_,_,_ in legs:dependencies[t].append(handle)
        except (GraphError,ValueError,KeyError,TypeError) as exc:
            excluded.append({"relation_id":r["relation_id"],"reason":str(exc)})
    return relations,excluded,dependencies


def compile_native(graph, model_sha):
    validate_graph(graph,model_sha)
    nodes=graph["nodes"]
    if len(nodes)>65536 or len(graph["relations"])>65536: raise GraphError("native_graph_capacity")
    relations,excluded,dependencies=_lower_relations(nodes,graph["relations"])
    lines=['#pragma once','#include "pm/v7_exact_arb_graph_hotpath.hpp"',
           'namespace pm::v7::exact_arb_graph::generated {',
           'inline constexpr char model_sha[] = "'+model_sha+'";',
           'inline constexpr char generation_sha256[] = "'+graph["graph_generation"]+'";',
           'inline const auto nodes = [] { std::array<CompiledNode, '+str(len(nodes))+'> a{};']
    for i,n in enumerate(nodes):
        octets=','.join(str(b) for b in bytes.fromhex(n["node_id"]))
        lines.append(f'a[{i}].book_handle={i}; a[{i}].identity_hash={{{octets}}};')
    lines+=['return a; }();',f'inline const auto relations = [] {{ std::array<CompiledRelation, {len(relations)}> a{{}};']
    for h,r,payout,reserve,legs,sell in relations:
        lines.append(f'a[{h}].relation_handle={h}; a[{h}].proof_handle={int(r["proof_hash"][:16],16)}ULL; '
                     f'a[{h}].guaranteed_payout_microunits={payout}LL; a[{h}].reserve_per_unit_microunits={reserve}LL; '
                     f'a[{h}].enabled=1; a[{h}].leg_count={len(legs)}; a[{h}].sell_inventory={int(sell)};')
        for i,(token,c,minimum,rate,exponent) in enumerate(legs):
            target=f'a[{h}].legs[{i}]'
            lines.append(f'{target}.book_handle={token}; {target}.coefficient={{{c.numerator}LL,{c.denominator}LL}}; '
                         f'{target}.minimum_order_microunits={minimum}LL; {target}.fee_rate={float(rate)!r}; '
                         f'{target}.fee_exponent={float(exponent)!r}; {target}.fee_verified=1;')
    handles=[h for group in dependencies for h in group]
    lines+=['return a; }();',f'inline const std::array<std::uint32_t, {len(handles)}> handles={{{",".join(map(str,handles))}}};',
            f'inline const std::array<TokenDependency, {len(nodes)}> dependencies={{{{']
    offset=0
    for token,group in enumerate(dependencies):
        lines.append(f'TokenDependency{{{token},{offset},{len(group)}}},');offset+=len(group)
    lines+=['}};', 'inline const GraphGeneration generation{',
            '{'+','.join(str(b) for b in bytes.fromhex(graph["graph_generation"]))+'}, nodes, relations, dependencies, handles};',
            '} // namespace generated']
    return '\n'.join(lines)+'\n',excluded


def compile_runtime_bundle(graph, model_sha, selected_relation_ids, selected_token_ids):
    """Project a proof-checked bounded subgraph, never expand the native bounds.

    Full graph validation and relation search are control-plane work. The wire
    body is canonical JSON text with its own digest; the loader must hash the
    EXACT bytes and recheck proofs/handles, not a platform-dependent re-encoding.
    A bundle is not actionable: the enclosing selection supplies a source lease
    and the native loader must map tokens to current causal decoder handles.
    """
    validate_graph(graph, model_sha)
    if len(selected_token_ids) > 128 or len(set(selected_token_ids)) != len(selected_token_ids):
        raise GraphError("native_runtime_token_capacity_or_collision")
    if len(selected_relation_ids) > 512 or len(set(selected_relation_ids)) != len(selected_relation_ids):
        raise GraphError("native_runtime_relation_capacity_or_collision")
    wanted_tokens=set(selected_token_ids)
    by_token={n["token_id"]:n for n in graph["nodes"] if n["token_id"] in wanted_tokens}
    if len(by_token) != sum(n["token_id"] in wanted_tokens for n in graph["nodes"]):
        raise GraphError("native_token_collision")
    # Binary partner subscriptions can include a token not needed by any graph
    # relation. Only claims present in the verified graph enter the kernel.
    nodes=[by_token[t] for t in selected_token_ids if t in by_token]
    wanted_relations=set(selected_relation_ids)
    by_id={r["relation_id"]:r for r in graph["relations"] if r["relation_id"] in wanted_relations}
    if set(by_id)!=wanted_relations or len(by_id)!=sum(r["relation_id"] in wanted_relations for r in graph["relations"]):
        raise GraphError("native_runtime_relation_identity")
    source=[by_id[rid] for rid in selected_relation_ids]
    lowered,excluded,dependencies=_lower_relations(nodes,source,max_relations=512)
    rows=[]
    for handle,relation,payout,reserve,legs,sell in lowered:
        compiled_legs=[]
        for index,(token,coefficient,minimum,rate,exponent) in enumerate(legs):
            leg=relation["legs"][index]
            tick=frac(leg["tick_size"])*10000 if leg.get("tick_size") is not None else None
            compiled_legs.append({
                "node_handle":token,
                "coefficient":[str(coefficient.numerator),str(coefficient.denominator)],
                "minimum_order_microunits":minimum,
                "fee_rate_nanos":int(rate*1000000000),"fee_exponent":int(exponent),
                "payout_vector":[[str(frac(v).numerator),str(frac(v).denominator)] for v in leg["payout_vector"]],
                "tick_size_e4":int(tick) if tick is not None and tick.denominator==1 else None,
            })
        rows.append({"relation_handle":handle,"relation_id":relation["relation_id"],
                     "economic_identity":relation["economic_identity"],
                     "relation_family":relation["relation_family"],"proof_hash":relation["proof_hash"],
                     "states":relation["states"],"guaranteed_payout_microunits":payout,
                     "reserve_per_unit_microunits":reserve,"sell_inventory":sell,
                     "settlement_close_ms":relation["settlement_close_ms"],"legs":compiled_legs})
    body={"schema":"polymarket_v7_exact_arb_native_bundle_v1",**SAFETY,
          "order_share_quantum_microunits":10000,
          "execution_authority":False,"model_sha":model_sha,"graph_generation":graph["graph_generation"],
          "nodes":[{"node_handle":i,"node_id":n["node_id"],"token_id":n["token_id"],
                    "condition_id":n.get("condition_id"),"market_id":n.get("market_id")} for i,n in enumerate(nodes)],
          "relations":rows,"dependencies":dependencies,"excluded":excluded}
    encoded=json.dumps(body,sort_keys=True,separators=(",",":"),allow_nan=False)
    return {"payload":encoded,"sha256":hashlib.sha256(encoded.encode()).hexdigest()}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--graph',type=Path,required=True);p.add_argument('--model-sha',required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    header,excluded=compile_native(load(a.graph),a.model_sha)
    a.output.write_text(header)
    import json
    print(json.dumps({'excluded':excluded,'output':str(a.output)}))


if __name__=='__main__':main()
