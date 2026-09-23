#!/usr/bin/env python3
"""Off-path compiler: validated graph JSON -> immutable bounded C++ arrays.

Unsupported families/fee terms are explicitly excluded, not approximated.
The generated data has no loader, file access or allocation on the hot path.
"""
from __future__ import annotations
import argparse
from fractions import Fraction
from pathlib import Path
from v7_unified_exact_arb_graph import GraphError, frac, load, validate_graph


def micro(value):
    scaled=frac(value)*1000000
    if scaled.denominator != 1 or not 0 <= scaled < 2**63: raise GraphError("native_precision_or_overflow")
    return int(scaled)


def compile_native(graph, model_sha):
    validate_graph(graph,model_sha)
    nodes=graph["nodes"]
    if len(nodes)>65536 or len(graph["relations"])>65536: raise GraphError("native_graph_capacity")
    tokens={node["token_id"]:i for i,node in enumerate(nodes)}
    if len(tokens)!=len(nodes): raise GraphError("native_token_collision")
    relations=[]; excluded=[]; dependencies=[[] for _ in nodes]
    for r in graph["relations"]:
        try:
            if not r["enabled"] or r["relation_type"]!="CONSTANT_PAYOUT_EQUALITY": raise GraphError("native_nonactionable")
            if not 1<=len(r["legs"])<=16: raise GraphError("native_cardinality")
            if r.get("transformation"): raise GraphError("native_transform_stage_requires_runtime_resource_evidence")
            payout,reserve=micro(r["guaranteed_payout"]),micro(r["reserve_per_unit"])
            legs=[]
            for leg in r["legs"]:
                coefficient=frac(leg["coefficient"])
                if not 0<coefficient.numerator<2**63 or not 0<coefficient.denominator<2**63: raise GraphError("native_coefficient")
                rate,exponent=frac(leg["fee_rate"]),frac(leg["fee_exponent"])
                if (not 0<=rate<=1 or not 0<=exponent<=2 or exponent.denominator!=1
                    or (rate*1000000000).denominator!=1): raise GraphError("native_fee")
                if (leg.get("fee_rounding_mode")!="VENUE_5DP" or frac(leg.get("fee_rounding_increment"))!=Fraction(1,100000)):
                    raise GraphError("native_fee_rounding_unsupported")
                legs.append((tokens[leg["token_id"]],coefficient,micro(leg["minimum_order"]),rate,exponent))
            directions=[d for d in r["directions"] if d in {
                "BUY_BASKET","BUY_COMPLETE_SET","SELL_INVENTORY_BASKET","SELL_COMPLETE_SET"}]
            # Preflight the whole relation: never publish BUY while rejecting
            # SELL halfway through the same immutable compilation unit.
            if len(relations)+len(directions)>65536: raise GraphError("native_graph_capacity")
            if any(len(dependencies[t])+len(directions)>64 for t,_,_,_,_ in legs):
                raise GraphError("native_dependency_capacity")
            for direction in directions:
                handle=len(relations)
                relations.append((handle,r,payout,reserve,legs,direction.startswith("SELL")))
                for t,_,_,_,_ in legs:dependencies[t].append(handle)
        except (GraphError,ValueError,KeyError,TypeError) as exc:
            excluded.append({"relation_id":r["relation_id"],"reason":str(exc)})
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


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--graph',type=Path,required=True);p.add_argument('--model-sha',required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    header,excluded=compile_native(load(a.graph),a.model_sha)
    a.output.write_text(header)
    import json
    print(json.dumps({'excluded':excluded,'output':str(a.output)}))


if __name__=='__main__':main()
