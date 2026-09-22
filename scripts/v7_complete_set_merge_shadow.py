#!/usr/bin/env python3
"""Zero-authority complete-set merge economics shadow.

For every paired YES+NO PAPER completion, build the exact merge candidate and
apply merge latency/cost only when a fresh independently verified evidence file
exists.  No private keys, relayer credentials, transaction signing or submission.

Until verification exists, the state is MERGE_ECONOMICS_UNVERIFIED and capital
remains locked to settlement in the allocator.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_pm_repricing_common import atomic_json

ROW_SCHEMA="polymarket_v7_complete_set_merge_shadow_cycle_v1"
STATUS_SCHEMA="polymarket_v7_complete_set_merge_shadow_status_v1"
EVIDENCE_SCHEMA="polymarket_v7_complete_set_merge_evidence_v1"
SELECTION_SCHEMA="polymarket_v7_multi_crypto_book_selection_v1"
STANDARD_ADAPTER="0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_ADAPTER="0xadA2005600Dec949baf300f4C6120000bDB6eAab"
PUSD="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


def load(path:Path|None)->dict[str,Any]:
    if path is None:return {}
    try:value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return value if isinstance(value,dict) else {}


def selection_map(value:dict[str,Any],sha:str)->dict[str,dict[str,Any]]:
    if (value.get("schema")!=SELECTION_SCHEMA or value.get("model_sha")!=sha
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") is not False):
        return {}
    return {str(row.get("market_id")):row for row in value.get("markets") or []
            if isinstance(row,dict) and row.get("market_id")}


def verified_evidence(value:dict[str,Any],sha:str,now_ms:int)->dict[str,Any]:
    if (value.get("schema")!=EVIDENCE_SCHEMA or value.get("model_sha")!=sha
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("verified") is not True):
        return {}
    try:
        latency=float(value.get("confirmed_latency_ms"))
        fixed=float(value.get("fixed_cost_pusd") or 0.0)
        bps=float(value.get("variable_cost_bps") or 0.0)
        expires=int(value.get("expires_at_ms") or 0)
    except (TypeError,ValueError,OverflowError):return {}
    if not(math.isfinite(latency) and latency>0 and math.isfinite(fixed) and fixed>=0
           and math.isfinite(bps) and bps>=0 and expires>=now_ms):
        return {}
    return {**value,"confirmed_latency_ms":latency,"fixed_cost_pusd":fixed,
            "variable_cost_bps":bps,"expires_at_ms":expires}


class Tail:
    def __init__(self,path:Path,sha:str):
        self.path,self.sha,self.handle=path,sha,None
    def poll(self)->list[dict[str,Any]]:
        out=[]
        for _ in range(2):
            if self.handle is None:
                try:self.handle=self.path.open("rb")
                except OSError:return out
            while True:
                pos=self.handle.tell();raw=self.handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    self.handle.seek(pos);break
                try:row=json.loads(raw)
                except (ValueError,UnicodeDecodeError):continue
                if (isinstance(row,dict)
                    and row.get("schema")=="polymarket_v7_pure_arb_exchange_execution_cycle_v1"
                    and row.get("model_sha")==self.sha
                    and row.get("paper_only") is True
                    and row.get("authenticated_execution") is False
                    and row.get("real_order_submission") is False):
                    out.append(row)
            try:
                old,cur=os.fstat(self.handle.fileno()),self.path.stat()
                if (old.st_dev,old.st_ino)==(cur.st_dev,cur.st_ino):
                    if cur.st_size<self.handle.tell():self.handle.seek(0)
                    return out
            except OSError:return out
            self.handle.close();self.handle=None
        return out


class Shadow:
    def __init__(self,args:argparse.Namespace):
        self.args=args;self.tail=Tail(args.cycles,args.model_sha)
        self.seen=set();self.rows=[]
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.status.parent.mkdir(parents=True,exist_ok=True)
        self._restore()

    def _restore(self)->None:
        try:
            for raw in self.args.output.read_text(encoding="utf-8").splitlines():
                row=json.loads(raw)
                if row.get("schema")==ROW_SCHEMA and row.get("model_sha")==self.args.model_sha:
                    self.rows.append(row);self.seen.add(str(row.get("scenario_id") or ""))
        except (OSError,json.JSONDecodeError):pass

    def evaluate(self,row:dict[str,Any])->dict[str,Any]|None:
        if row.get("state")!="COMPLETE_PAIRED":return None
        # BUY_COMPLETE_SET creates new equal YES+NO inventory. SELL_COMPLETE_SET
        # consumes prefunded inventory and therefore leaves nothing to merge.
        if str(row.get("kind") or "")!="BUY_COMPLETE_SET":return None
        scenario=str(row.get("scenario_id") or "")
        if not scenario or scenario in self.seen:return None
        self.seen.add(scenario)
        mid=str(row.get("market_id") or "")
        market=selection_map(load(self.args.selection),self.args.model_sha).get(mid)
        q=float(row.get("target_shares") or 0.0)
        base={
            "schema":ROW_SCHEMA,"model_sha":self.args.model_sha,
            "paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_MERGE_SHADOW",
            "scenario_id":scenario,"market_id":mid,"mergeable_shares":q,
            "merge_value_pusd":q,"state":"MERGE_METADATA_UNVERIFIED",
        }
        if market is None or q<=0:return base
        condition=str(market.get("condition_id") or "")
        if not condition:return base
        neg=market.get("neg_risk") is True or market.get("negRisk") is True
        adapter=NEG_RISK_ADAPTER if neg else STANDARD_ADAPTER
        base.update({
            "condition_id":condition,"neg_risk":neg,"adapter":adapter,
            "collateral_token":PUSD,"parent_collection_id":"0x"+"00"*32,
            "partition":[1,2],
            "prepared_operation":"mergePositions(address,bytes32,bytes32,uint256[],uint256)",
            "prepared_amount_base_units":int(math.floor(q*1_000_000+1e-9)),
            "operation_atomic":True,
            "state":"MERGE_ECONOMICS_UNVERIFIED",
        })
        evidence=verified_evidence(load(self.args.evidence),self.args.model_sha,time.time_ns()//1_000_000)
        if not evidence:return base
        capital=max(0.0,float(row.get("revalidation_yes_price") or 0.0)
                    +float(row.get("revalidation_no_price") or 0.0))*q
        cost=float(evidence["fixed_cost_pusd"])+capital*float(evidence["variable_cost_bps"])/10_000.0
        base.update({
            "state":"MERGE_ECONOMICS_VERIFIED",
            "confirmed_latency_ms":evidence["confirmed_latency_ms"],
            "merge_cost_pusd":cost,
            "evidence_source":str(evidence.get("source") or ""),
            "evidence_expires_at_ms":evidence["expires_at_ms"],
        })
        return base

    def publish(self)->None:
        states=Counter(str(row.get("state")) for row in self.rows)
        atomic_json(self.args.status,{
            "schema":STATUS_SCHEMA,"version":1,"model_sha":self.args.model_sha,
            "paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"execution_authority":"ZERO_AUTHORITY_MERGE_SHADOW",
            "timestamp_ms":time.time_ns()//1_000_000,"cycles":len(self.rows),
            "states":dict(states),
            "verified_cycles":states.get("MERGE_ECONOMICS_VERIFIED",0),
            "unverified_cycles":states.get("MERGE_ECONOMICS_UNVERIFIED",0),
        })

    def run(self)->None:
        while True:
            for row in self.tail.poll():
                result=self.evaluate(row)
                if result is None:continue
                with self.args.output.open("a",encoding="utf-8") as handle:
                    handle.write(json.dumps(result,sort_keys=True)+"\n")
                self.rows.append(result)
            self.publish()
            time.sleep(max(0.05,self.args.interval_ms/1000.0))


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles",type=Path,required=True)
    ap.add_argument("--selection",type=Path,required=True)
    ap.add_argument("--evidence",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--interval-ms",type=int,default=100)
    args=ap.parse_args()
    if len(args.model_sha)!=40 or not 10<=args.interval_ms<=10_000:
        raise SystemExit("invalid arguments")
    Shadow(args).run();return 0


if __name__=="__main__":
    raise SystemExit(main())
