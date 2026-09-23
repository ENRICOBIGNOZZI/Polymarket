#!/usr/bin/env python3
"""Local deterministic benchmark; timed loops perform no I/O or JSON parsing."""
from __future__ import annotations
import argparse,json,time
from pathlib import Path
from v7_unified_exact_arb_graph import evaluate

def percentile(v:list[int])->dict[str,int]:
 v.sort();return {k:v[min(len(v)-1,int((len(v)-1)*p))] for k,p in (("p50",.5),("p90",.9),("p99",.99),("p99_9",.999))}|{"max":v[-1]}
def relation(n:int)->dict:
 return {"enabled":True,"guaranteed_payout":"1","reserve_per_unit":"0","legs":[{"token_id":f"t{i}","coefficient":"1","minimum_order":"0"} for i in range(n)]}
def books(n:int,price:str)->dict:
 return {f"t{i}":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0","asks":[[price,"100"]]} for i in range(n)}
def timed(fn,iterations:int)->dict[str,int]:
 samples=[]
 for _ in range(iterations):
  started=time.perf_counter_ns();fn();samples.append(time.perf_counter_ns()-started)
 return percentile(samples)
def bench(n:int,price:str,iterations:int)->dict[str,dict[str,int]]:
 r,b=relation(n),books(n,price);index={"t0":[0]}
 # Each timer is deliberately independent: lookup measures only the compact
 # dependency access, sizing measures the complete full-depth relation, and
 # total is the token-update composition used by the shadow contract.
 return {"dependency_lookup":timed(lambda:index.get("t0",()),iterations),
         "relation_evaluation":timed(lambda:evaluate(r,b,1),iterations),
         "sizing":timed(lambda:evaluate(r,b,1),iterations),
         "total_decision_compute":timed(lambda:(index.get("t0",()),evaluate(r,b,1)),iterations)}
def main()->int:
 p=argparse.ArgumentParser();p.add_argument("--iterations",type=int,default=1000);p.add_argument("--output",type=Path);a=p.parse_args()
 if a.iterations<100:raise SystemExit("iterations must be >=100")
 # Frozen direct touch baseline; the graph evaluator remains a separate shadow.
 direct=[]
 for _ in range(a.iterations):
  t=time.perf_counter_ns();_ = 1.0-.49-.49;direct.append(time.perf_counter_ns()-t)
 value={"schema":"polymarket_v7_unified_exact_arb_graph_benchmark_v1","paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,"automatic_promotion":False,"iterations":a.iterations,"nanoseconds":{"binary_champion_touch":{"total_decision_compute":percentile(direct)},"binary_graph":bench(2,".49",a.iterations),"n_leg_graph":bench(4,".24",a.iterations)}}
 text=json.dumps(value,sort_keys=True)+"\n"
 if a.output:a.output.write_text(text)
 else:print(text,end="")
 return 0
if __name__=="__main__":raise SystemExit(main())
