#!/usr/bin/env python3
"""Read-only incremental evaluator for compiled exact-arbitrage graph generations."""
from __future__ import annotations
import argparse,json,os,time
from collections import Counter,defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any
from v7_unified_exact_arb_graph import SAFETY,evaluate

SCHEMA="polymarket_v7_unified_exact_arb_graph_shadow_status_v1"

def load(path:Path)->dict[str,Any]:
    try:v=json.loads(path.read_text())
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}

def atomic(path:Path,value:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,sort_keys=True)+"\n");os.replace(tmp,path)

def native_binary_candidate(relation:dict[str,Any], result:dict[str,Any], now:int)->dict[str,Any]|None:
 """Narrow adapter into the proven two-leg execution shadow, never an order."""
 legs=relation.get("legs")
 if not isinstance(legs,list) or len(legs)!=2:return None
 markets={str(x.get("market_id") or "") for x in legs}
 outcomes={str(x.get("outcome") or "").upper() for x in legs}
 if len(markets)!=1 or not next(iter(markets)) or outcomes!={"YES","NO"}:return None
 direction=str(result.get("direction") or "")
 if direction not in {"BUY","SELL"}:return None
 return {"schema":"polymarket_v7_unified_exact_arb_graph_execution_candidate_v1",**SAFETY,
         "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","graph_relation_id":relation.get("relation_id"),
         "market_id":next(iter(markets)),"kind":"BUY_COMPLETE_SET" if direction=="BUY" else "SELL_COMPLETE_SET",
         "receive_wall_ms":now,"executable_shares_local_deep":result.get("quantity"),
         "minimum_order_shares":"0","source":"UNIFIED_EXACT_ARB_GRAPH_BINARY_ADAPTER"}

class Shadow:
 def __init__(self,a:argparse.Namespace):
  self.a=a;self.offset=0;self.books={};self.generation="";self.generation_compiled_at_ms=0;self.relations=[];self.index={};self.funnel=Counter();self.funnel_by_family=defaultdict(Counter);self.rejects=Counter();self.rejects_by_family=defaultdict(Counter);self.dist=defaultdict(list);self.seen=set();self.pending=[];self.claims={};self.capital_reserved={};self.latency_us=[];self.capital_limit="0"
 def capital(self):
  path=getattr(self.a,"capital_policy",None)
  if path is None:return str(getattr(self.a,"capital_limit","0"))
  value=load(path)
  if (value.get("schema")!="polymarket_v7_pure_arb_capital_policy_v1" or value.get("paper_only") is not True
      or value.get("authenticated_execution") is not False or value.get("real_order_submission") is not False):return "0"
  try:
   limit=float(value["paper_budget_pusd"])
   if limit<=0 or limit!=limit:return "0"
   return str(value["paper_budget_pusd"])
  except (KeyError,TypeError,ValueError):return "0"
 def graph(self):
  g=load(self.a.graph)
  if g.get("schema")!="polymarket_v7_unified_exact_arb_graph_v1" or any(g.get(k) is not v for k,v in SAFETY.items()):return
  if g.get("graph_generation")!=self.generation:
   self.generation=str(g.get("graph_generation") or "");self.relations=g.get("relations") or [];self.index=g.get("dependency_index") or {}
   try:self.generation_compiled_at_ms=int((g.get("metadata") or {}).get("compiled_at_ms") or 0)
   except (TypeError,ValueError):self.generation_compiled_at_ms=0
 def update(self,row:dict[str,Any]):
  if (row.get("schema")!="polymarket_v7_pure_arb_deep_book_snapshot_v1" or row.get("model_sha")!=self.a.model_sha or row.get("paper_only") is not True or row.get("authenticated_execution") is not False or row.get("real_order_submission") is not False or row.get("execution_authority")!="ZERO_AUTHORITY_RESEARCH_ONLY"):return
  try: now=int(row["receive_wall_ms"]); mid=str(row["market_id"]);pairs=(("yes_token","yes_ask_levels","yes_bid_levels","yes_ask_truncated"),("no_token","no_ask_levels","no_bid_levels","no_ask_truncated"))
  except (KeyError,TypeError,ValueError):return
  changed=[]
  for token,levels,bids,truncated in pairs:
   t=str(row.get(token) or "")
   if not t:continue
   self.books[t]={"timestamp_ms":now,"lineage_continuous":True,"depth_truncated":row.get(truncated) is True,"asks":[[x.get("price"),x.get("size")] for x in row.get(levels) or []],"bids":[[x.get("price"),x.get("size")] for x in row.get(bids) or []]};changed.append(t)
  pending,self.pending=self.pending,[]
  for due,h,arm in pending:
   if now<due:self.pending.append((due,h,arm));continue
   try:survived=evaluate(self.relations[h],self.books,now).get("accepted") is True
   except (IndexError,KeyError,TypeError):survived=False
   self.funnel["survival_"+str(arm)+"ms_checked"]+=1
   if survived:self.funnel["survival_"+str(arm)+"ms"]+=1
  event_seen=set()
  for token in changed:
   for h in self.index.get(token,[]):
    relation=self.relations[h];family=str(relation.get("relation_family") or "UNKNOWN")
    self.funnel["relations_considered"]+=1;self.funnel_by_family[family]["relations_considered"]+=1
    started=time.perf_counter_ns()
    try:r=evaluate(relation,self.books,now,capital_limit=self.capital())
    except Exception:r={"accepted":False,"reason":"evaluation_error"}
    self.latency_us.append((time.perf_counter_ns()-started)/1000.0)
    if len(self.latency_us)>100000:self.latency_us=self.latency_us[-50000:]
    reason=str(r.get("reason") or "unknown")
    # The evaluator returns the first failed gate.  Count all preceding gates
    # so the funnel is monotonic and remains useful when no candidate exists.
    stages=("books_ready","lineage_ready","fee_ready","freshness_ready","leg_skew_ready")
    failed={"lineage_or_book_missing":0,"truncated_depth":0,"fee_or_timestamp_missing":2,
            "fee_or_depth_invalid":2,"fee_rounding_invalid":2,"stale_book":3,"leg_skew":4,
            "depth_insufficient":5,"minimum_order":5,"capital_limit":5,"inventory_unavailable":5,"transformation_capacity":5,
            "empty_relation":0,"disabled_relation":0}.get(reason,5)
    if not r.get("accepted"):
     for stage in stages[:failed]:self.funnel[stage]+=1;self.funnel_by_family[family][stage]+=1
    for field in ("distance_to_raw_arbitrage","distance_to_after_fee_arbitrage","distance_to_after_reserve_arbitrage"):
     try:self.dist[family+":"+field].append(float(r[field]))
     except (KeyError,TypeError,ValueError):pass
    for field,name in (("distance_to_raw_arbitrage","raw_positive"),("distance_to_after_fee_arbitrage","after_fee_positive"),("distance_to_after_reserve_arbitrage","after_reserve_positive")):
     try:
      if float(r.get(field,0))<0:self.funnel[name]+=1
     except (TypeError,ValueError):pass
    if r.get("accepted"):
     for stage in ("books_ready","lineage_ready","fee_ready","freshness_ready","leg_skew_ready","depth_sufficient","minimum_order_sufficient","capital_sufficient","transformation_ready","execution_semantics_ready"):
      self.funnel[stage]+=1;self.funnel_by_family[family][stage]+=1
     key=str(relation.get("economic_identity"))+":"+str(r.get("direction") or "BUY")+":"+str(now)
     self.funnel["raw_path_count"]+=1;self.funnel_by_family[family]["raw_path_count"]+=1
     if key not in event_seen:
      claims=set()
      if str(r.get("direction") or "") == "SELL":
       claims.update("inventory:"+str(leg.get("token_id") or "") for leg in relation.get("legs") or [])
      transform=relation.get("transformation") if isinstance(relation.get("transformation"),dict) else None
      if transform is not None:claims.add("transformation:"+str(transform.get("id") or ""))
      conflict=any((now,claim) in self.claims for claim in claims)
      try:required,limit=Fraction(str(r["capital_required"])),Fraction(self.capital())
      except (KeyError,TypeError,ValueError,ZeroDivisionError):required,limit=Fraction(1),Fraction(0)
      reserved=self.capital_reserved.get(now,Fraction(0))
      capital_conflict=reserved+required>limit
      if conflict or capital_conflict:
       event_seen.add(key)
       self.funnel["capital_conflicts"]+=1;self.funnel_by_family[family]["capital_conflicts"]+=1
       self.rejects["capital_conflict"]+=1;self.rejects_by_family[family]["capital_conflict"]+=1
       continue
      event_seen.add(key);self.funnel["candidate_emitted"]+=1
      self.funnel["unique_economic_opportunity_count"]+=1;self.funnel_by_family[family]["unique_economic_opportunity_count"]+=1
      for claim in claims:self.claims[(now,claim)]=key
      self.capital_reserved[now]=reserved+required
      evidence={"schema":"polymarket_v7_unified_exact_arb_graph_opportunity_v1",**SAFETY,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","graph_generation":self.generation,"relation_id":relation.get("relation_id"),"trigger_token":token,"timestamp_ms":now,"counterfactual_modes":["SEQUENTIAL","PARALLEL","BATCH"],"capital_conflict":False,"execution_candidate":native_binary_candidate(relation,r,now),"result":r}
      with self.a.opportunities.open("a") as f:f.write(json.dumps(evidence,sort_keys=True)+"\n")
      for arm in (1,5,10,25,50):self.pending.append((now+arm,h,arm))
     else:
      self.funnel["deduplicated_path_count"]+=1;self.funnel_by_family[family]["deduplicated_path_count"]+=1
    else:self.rejects[reason]+=1;self.rejects_by_family[family][reason]+=1
 def run(self):
  self.a.opportunities.parent.mkdir(parents=True,exist_ok=True);next_status=0.
  while True:
   self.graph()
   try:
    with self.a.tape.open("rb") as f:
     f.seek(self.offset)
     for raw in f:
      if not raw.endswith(b"\n"):break
      self.offset=f.tell()
      try:self.update(json.loads(raw))
      except (ValueError,UnicodeDecodeError):continue
   except OSError:pass
   if time.monotonic()>=next_status:
    def pct(v):
     v=sorted(v);return {str(p):v[min(len(v)-1,max(0,int(len(v)*p/100)-1))] for p in (.1,1,5,10,25,50,90,99)}|{"min":v[0]} if v else {}
    atomic(self.a.status,{"schema":SCHEMA,"model_sha":self.a.model_sha,**SAFETY,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","state":"COLLECTING","graph_generation":self.generation,"graph_generation_compiled_at_ms":self.generation_compiled_at_ms,"relations_compiled":len(self.relations),"relations_evaluated":self.funnel["relations_considered"],"capital_limit_pusd":self.capital(),"funnel":dict(self.funnel),"funnel_by_family":{k:dict(v) for k,v in self.funnel_by_family.items()},"rejection_reasons":dict(self.rejects),"rejection_reasons_by_family":{k:dict(v) for k,v in self.rejects_by_family.items()},"near_arbitrage":{k:pct(v) for k,v in self.dist.items()},"evaluation_latency_us":pct(self.latency_us),"counterfactual":{"filled":0,"one_leg_exposure":0,"unwind":0,"unwind_loss":0,"realized_pnl":0},"timestamp_ms":time.time_ns()//1_000_000});next_status=time.monotonic()+1
   time.sleep(max(.001,self.a.interval_ms/1000))
def main()->int:
 p=argparse.ArgumentParser();p.add_argument("--graph",type=Path,required=True);p.add_argument("--tape",type=Path,required=True);p.add_argument("--status",type=Path,required=True);p.add_argument("--opportunities",type=Path,required=True);p.add_argument("--capital-policy",type=Path,required=True);p.add_argument("--model-sha",required=True);p.add_argument("--interval-ms",type=int,default=10);a=p.parse_args()
 if len(a.model_sha)!=40 or not 1<=a.interval_ms<=1000:raise SystemExit("invalid arguments")
 Shadow(a).run();return 0
if __name__=="__main__":raise SystemExit(main())
