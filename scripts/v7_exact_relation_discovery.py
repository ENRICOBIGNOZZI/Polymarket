#!/usr/bin/env python3
"""Discover and prove exact deterministic payout relations from the live universe.

Zero-authority research only.  A generated relation is admissible only when its
finite-state payout vectors sum exactly to a constant guarantee.  Automatic
discovery is intentionally limited to payoff-identical duplicate markets;
anything more general must arrive as an explicit candidate relation and pass the
same exact rational-arithmetic proof.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import time
from typing import Any

SCHEMA="polymarket_v7_exact_arb_relation_registry_v1"
PROOF_SCHEMA="polymarket_v7_exact_relation_proof_v1"


def load(path:Path)->dict[str,Any]:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}


def frac(value:Any)->Fraction:
    if isinstance(value,bool):raise ValueError
    return Fraction(str(value))


def prove_relation(row:dict[str,Any])->dict[str,Any]:
    states=row.get("states"); legs=row.get("legs")
    guarantee=frac(row.get("guaranteed_payout"))
    if not isinstance(states,list) or not states or len(states)>64:
        raise ValueError("invalid states")
    if not isinstance(legs,list) or len(legs)<2 or len(legs)>32:
        raise ValueError("invalid legs")
    totals=[Fraction(0) for _ in states]
    normalized=[]
    for leg in legs:
        if not isinstance(leg,dict):raise ValueError("invalid leg")
        vector=leg.get("payout_vector")
        coefficient=frac(leg.get("coefficient",1))
        if coefficient<=0 or not isinstance(vector,list) or len(vector)!=len(states):
            raise ValueError("invalid payout vector")
        payouts=[frac(x) for x in vector]
        if any(x<0 for x in payouts):raise ValueError("negative payout")
        for i,x in enumerate(payouts):totals[i]+=coefficient*x
        normalized.append({
            "selector":leg.get("selector"),"outcome":str(leg.get("outcome") or "").upper(),
            "coefficient":str(coefficient),
            "payout_vector":[str(x) for x in payouts],
        })
    if any(x!=guarantee for x in totals):
        raise ValueError("non-constant basket payout")
    body=json.dumps({
        "states":[str(x) for x in states],"guarantee":str(guarantee),
        "legs":normalized,
    },sort_keys=True,separators=(",",":"))
    return {
        "schema":PROOF_SCHEMA,
        "proof_type":"FINITE_STATE_EXACT_RATIONAL_SUM",
        "constant_payout":str(guarantee),
        "state_totals":[str(x) for x in totals],
        "proof_sha256":hashlib.sha256(body.encode()).hexdigest(),
    }


def selector(row:dict[str,Any])->dict[str,Any]:
    return {
        "market_id":str(row.get("market_id") or ""),
        "asset":str(row.get("asset") or ""),
        "horizon":str(row.get("horizon") or ""),
        "contract_family":str(row.get("contract_family") or ""),
        "settlement_semantic_hash":str(row.get("settlement_semantic_hash") or ""),
        "window_start_unix":int(row.get("window_start_unix") or 0),
        "close_timestamp_unix":int(row.get("close_timestamp_unix") or 0),
    }


def identity(row:dict[str,Any])->tuple[Any,...]|None:
    s=selector(row)
    if not (s["market_id"] and s["asset"] and s["horizon"] and s["contract_family"]
            and len(s["settlement_semantic_hash"])==64 and s["window_start_unix"]>0
            and s["close_timestamp_unix"]>s["window_start_unix"]):
        return None
    return (s["asset"],s["horizon"],s["contract_family"],
            s["settlement_semantic_hash"],s["window_start_unix"],s["close_timestamp_unix"])


def duplicate_relation(a:dict[str,Any],b:dict[str,Any],first:str)->dict[str,Any]:
    sa,sb=selector(a),selector(b)
    rid="dup-"+hashlib.sha256(
        json.dumps([sa["market_id"],sb["market_id"],first],sort_keys=True).encode()
    ).hexdigest()[:24]
    if first=="YES_A_NO_B":
        legs=[
            {"selector":sa,"outcome":"YES","coefficient":1,"payout_vector":[1,0]},
            {"selector":sb,"outcome":"NO","coefficient":1,"payout_vector":[0,1]},
        ]
    else:
        legs=[
            {"selector":sa,"outcome":"NO","coefficient":1,"payout_vector":[0,1]},
            {"selector":sb,"outcome":"YES","coefficient":1,"payout_vector":[1,0]},
        ]
    row={"id":rid,"enabled":True,"states":["UP","DOWN"],"guaranteed_payout":1,"legs":legs,
         "discovery":"AUTOMATIC_PAYOFF_IDENTICAL_DUPLICATE"}
    row["proof"]=prove_relation(row)
    return row


def build(universe:dict[str,Any],model_sha:str)->dict[str,Any]:
    if (universe.get("paper_only") is not True
        or universe.get("authenticated_execution") is not False
        or universe.get("real_order_submission") is not False
        or universe.get("model_sha")!=model_sha):
        raise ValueError("unsafe universe")
    groups={}
    for row in universe.get("markets") or []:
        if not isinstance(row,dict) or row.get("active") is not True or row.get("closed") is True:
            continue
        key=identity(row)
        if key is not None:groups.setdefault(key,[]).append(row)
    relations=[]
    for rows in groups.values():
        rows=sorted(rows,key=lambda x:str(x.get("market_id") or ""))
        for i in range(len(rows)):
            for j in range(i+1,len(rows)):
                if str(rows[i].get("market_id"))==str(rows[j].get("market_id")):continue
                relations.append(duplicate_relation(rows[i],rows[j],"YES_A_NO_B"))
                relations.append(duplicate_relation(rows[i],rows[j],"NO_A_YES_B"))
    relations.sort(key=lambda x:x["id"])
    return {
        "schema":SCHEMA,"version":1,"paper_only":True,
        "authenticated_execution":False,"real_order_submission":False,
        "automatic_promotion":False,"model_sha":model_sha,
        "generated_at_ms":time.time_ns()//1_000_000,
        "semantics":"AUTO_DISCOVERY_ONLY_FOR_PAYOFF_IDENTICAL_DUPLICATES; ALL_RELATIONS_EXACT_RATIONAL_PROOF",
        "relations":relations,
    }


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--output",type=Path,required=True)
    args=ap.parse_args()
    if len(args.model_sha)!=40:raise SystemExit("invalid sha")
    value=build(load(args.universe),args.model_sha)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    tmp=args.output.with_suffix(args.output.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    tmp.replace(args.output)
    return 0

if __name__=="__main__":raise SystemExit(main())
