#!/usr/bin/env python3
"""Read-only incremental evaluator for compiled exact-arbitrage graph generations."""
from __future__ import annotations
import argparse,json,os,time
from collections import Counter,defaultdict,deque
from fractions import Fraction
from pathlib import Path
from typing import Any
from v7_unified_exact_arb_graph import SAFETY,GraphError,evaluate,frac,sha,validate_graph,safe
from v7_exact_arb_causal import CausalBooks,ResourceLedger,ReconstructedDepth,JsonlCursor,decode_snapshot,quantiles
from v7_exact_arb_source_health import graph_lease

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
  self.a=a; self.offset=0; self.tape_identity=None; self.books={}; self.history=CausalBooks()
  self.generation=""; self.generation_compiled_at_ms=0; self.relations=[]; self.index={}
  self.funnel=Counter(); self.funnel_by_family=defaultdict(Counter)
  self.rejects=Counter(); self.rejects_by_family=defaultdict(Counter)
  self.dist=defaultdict(lambda:deque(maxlen=100000)); self.distance_min={}; self.latency_us=deque(maxlen=100000)
  self.survival_evidence=deque(maxlen=10000)
  self.lifetimes=defaultdict(lambda:deque(maxlen=100000)); self.active={}; self.pending=[]
  self.resources=ResourceLedger(); self.seen=set(); self.emitted=set(); self.last_event_ms=0
  self.recovered_generations=set()
  self.events_processed=0; self.dropped=Counter(); self.graph_state="WAITING_FOR_GRAPH"
  self.last_candidate_ms=None; self.graph_metadata={}; self.last_file_signature=None
  self.reconstruction=ReconstructedDepth(a.model_sha); self.delta_offset=0
  # Deterministic tape replay reconstructs counters and reservations. Durable
  # IDs suppress already-written evidence after a crash (including append
  # succeeded / checkpoint did not). A damaged output is an explicit blocker.
  if a.opportunities.exists():
   for line in a.opportunities.open():
    row=json.loads(line)
    if row.get("model_sha")!=a.model_sha: raise GraphError("recovery_model_mismatch")
    self.emitted.add(row["opportunity_id"])
    self.recovered_generations.add(row["graph_generation"])

 def capital(self):
  path=getattr(self.a,"capital_policy",None)
  if path is None:return str(getattr(self.a,"capital_limit","0"))
  value=load(path)
  if (value.get("schema")!="polymarket_v7_pure_arb_capital_policy_v1" or value.get("paper_only") is not True
      or value.get("authenticated_execution") is not False or value.get("real_order_submission") is not False):return "0"
  try:
   limit=frac(value["paper_budget_pusd"])
   return str(limit) if limit>0 else "0"
  except (KeyError,TypeError,ValueError):return "0"

 def graph(self,as_of_ms=None):
  try:
   stat=self.a.graph.stat(); signature=(stat.st_ino,stat.st_mtime_ns,stat.st_size)
   if signature==self.last_file_signature:
    if as_of_ms is not None:graph_lease(self.graph_metadata,as_of_ms)
    self.graph_state="COLLECTING"
    return
   g=load(self.a.graph); validate_graph(g,self.a.model_sha)
   if as_of_ms is not None:graph_lease(g,as_of_ms)
   if not self.generation and self.recovered_generations and self.recovered_generations!={g["graph_generation"]}:
    raise GraphError("recovery_generation_mismatch")
  except (OSError,GraphError,KeyError,TypeError,ValueError):
   self.graph_state="BLOCKED_INVALID_GRAPH"; return
  if g["graph_generation"]!=self.generation:
   if self.generation:
    self.dropped["generation_pending_censored"]+=len(self.pending)
    self.pending=[]; self.active={}
   self.generation=g["graph_generation"]; self.relations=g["relations"]; self.index=g["dependency_index"]
   self.graph_metadata=g; self.generation_compiled_at_ms=int(g["metadata"]["compiled_at_ms"])
  self.last_file_signature=signature; self.graph_state="COLLECTING"

 def count(self,family,key,amount=1):
  self.funnel[key]+=amount; self.funnel_by_family[family][key]+=amount

 def resource_capacities(self,now):
  result={"PUSD":self.capital()}
  path=getattr(self.a,"resource_snapshot",None)
  if path is None:return result
  value=load(path)
  if (not safe(value,self.a.model_sha) or value.get("schema")!="polymarket_v7_exact_arb_paper_resources_v1"
      or value.get("verified") is not True or not value.get("timestamp_ms",0)<=now<=value.get("expires_at_ms",0)):
   return result
  try:
   for key,amount in value.get("capacities",{}).items():
    if key.startswith(("inventory:","transformation:")) and frac(amount)>=0:result[key]=str(frac(amount))
  except (TypeError,ValueError):return {"PUSD":self.capital()}
  return result

 def survival(self,now):
  remaining=[]
  for due,relation,direction,quantity,arm,generation in self.pending:
   if due>now:remaining.append((due,relation,direction,quantity,arm,generation));continue
   family=relation.get("relation_family","UNKNOWN")
   causal={leg["token_id"]:self.history.at(leg["token_id"],due) for leg in relation["legs"]}
   r=evaluate({**relation,"directions":["BUY_BASKET" if direction=="BUY" else "SELL_INVENTORY_BASKET"]},
              causal,due,capital_limit=self.capital(),inventory_limit=quantity if direction=="SELL" else None)
   self.count(family,"survival_"+str(arm)+"ms_checked")
   if r.get("accepted"):self.count(family,"survives_"+str(arm)+"ms")
   self.survival_evidence.append({"graph_generation":generation,"relation_id":relation["relation_id"],
      "due_timestamp_ms":due,"delay_ms":arm,"direction":direction,
      "still_raw_arb":frac(r["distance_to_raw_arbitrage"])<0 if "distance_to_raw_arbitrage" in r else None,
      "still_after_fee":frac(r["distance_to_after_fee_arbitrage"])<0 if "distance_to_after_fee_arbitrage" in r else None,
      "still_after_reserve":frac(r["distance_to_after_reserve_arbitrage"])<0 if "distance_to_after_reserve_arbitrage" in r else None,
      "still_executable":r.get("accepted",False),"q_remaining":r.get("quantity","0"),
      "locked_pnl_remaining":r.get("net_locked_pnl","0"),"reason":r.get("reason")})
  self.pending=remaining

 def update(self,row):
  try:now,new_books=decode_snapshot(row,self.a.model_sha)
  except (GraphError,KeyError,TypeError,ValueError):
   self.dropped["snapshot_invalid"]+=1;return
  self.update_decoded(now,new_books,sha(row))

 def update_decoded(self,now,new_books,event_id):
  if now<self.last_event_ms:self.dropped["timestamp_reversal"]+=1;return
  if event_id in self.seen:return
  # Dedup only the current timestamp; evidence IDs below span restarts.
  if now>self.last_event_ms:self.seen.clear()
  self.seen.add(event_id)
  try:self.history.ingest(now,new_books)
  except GraphError:self.dropped["history_invalid"]+=1;return
  self.books.update(new_books);self.last_event_ms=now;self.events_processed+=1
  self.resources.release(now)
  self.survival(now)
  event_seen=set()
  affected=sorted({h for token in new_books for h in self.index.get(token,[])})
  capital=self.capital()  # Control-plane read once per event, not once per leg.
  resource_capacities=self.resource_capacities(now)
  for h in affected:
   relation=self.relations[h];family=relation.get("relation_family","UNKNOWN")
   self.count(family,"relations_considered")
   started=time.perf_counter_ns()
   if relation.get("settlement_close_ms",0) and now>=relation["settlement_close_ms"]:
    r={"accepted":False,"reason":"market_closed"}
   else:
    try:
     inventory=None
     used=self.resources.used()
     if all("inventory:"+leg["token_id"] in resource_capacities for leg in relation["legs"]):
      inventory=min(max(Fraction(0),frac(resource_capacities["inventory:"+leg["token_id"]])-used["inventory:"+leg["token_id"]])/frac(leg["coefficient"])
                    for leg in relation["legs"])
     r=evaluate(relation,self.books,now,capital_limit=capital,inventory_limit=inventory)
    except (GraphError,KeyError,TypeError,ValueError):r={"accepted":False,"reason":"evaluation_error"}
   self.latency_us.append((time.perf_counter_ns()-started)/1000)
   reason=r.get("reason","unknown")
   stages=("books_ready","lineage_ready","fee_ready","freshness_ready","leg_skew_ready")
   failed={"lineage_or_book_missing":0,"truncated_depth":0,"fee_or_timestamp_missing":2,
           "fee_or_depth_invalid":2,"fee_rounding_invalid":2,"stale_book":3,"leg_skew":4,
           "fee_changed":2,"tick_changed":2,"duplicate_token_claim":0,
           "disabled_relation":0,"market_closed":0,"evaluation_error":0}.get(reason,5)
   for stage in stages[:failed]:self.count(family,stage)
   for field,name in (("distance_to_raw_arbitrage","raw_positive"),("distance_to_after_fee_arbitrage","after_fee_positive"),
                      ("distance_to_after_reserve_arbitrage","after_reserve_positive")):
    if field not in r:continue
    value=frac(r[field]);self.dist[family+":"+field].append(value)
    if value<0:self.count(family,name)
    guarantee=frac(relation["guaranteed_payout"])
    if guarantee>0:self.dist[family+":"+field+"_bps"].append(value*10000/guarantee)
    ticks=[frac(leg["tick_size"])*frac(leg["coefficient"]) for leg in relation["legs"] if leg.get("tick_size")]
    if len(ticks)==len(relation["legs"]):
     self.dist[family+":"+field+"_ticks"].append(value/min(ticks))
    for metric in (field,field+"_bps",field+"_ticks"):
     k=family+":"+metric
     if self.dist[k]:self.distance_min[k]=min(self.distance_min.get(k,self.dist[k][-1]),self.dist[k][-1])
   economic=str(relation.get("economic_identity"))
   if not r.get("accepted"):
    self.rejects[reason]+=1;self.rejects_by_family[family][reason]+=1
    if economic in self.active:
     first,last=self.active.pop(economic);self.lifetimes[family].append(last-first)
    continue
   if economic not in self.active:self.active[economic]=(now,now)
   else:self.active[economic]=(self.active[economic][0],now)
   for stage in ("depth_sufficient","minimum_order_sufficient","capital_sufficient","transformation_ready","execution_semantics_ready"):
    self.count(family,stage)
   self.count(family,"raw_path_count")
   key=economic+":"+r["direction"]
   if key in event_seen:self.count(family,"deduplicated_path_count");continue
   event_seen.add(key)
   capacities=dict(resource_capacities);required={"PUSD":r["capital_required"]}
   if r["direction"]=="SELL":
    for leg in relation["legs"]:required["inventory:"+leg["token_id"]]=str(frac(r["quantity"])*frac(leg["coefficient"]))
   transform=relation.get("transformation")
   if transform:
    resource="transformation:"+transform["id"]
    capacities[resource]=transform["capacity"];required[resource]=r["quantity"]
   # Unknown settlement release time remains reserved indefinitely.
   opportunity_id=sha([self.generation,event_id,key])
   if not self.resources.reserve(opportunity_id,required,capacities,now,r.get("capital_lock_time_ms")):
    self.count(family,"capital_conflicts");self.rejects["capital_conflict"]+=1
    self.rejects_by_family[family]["capital_conflict"]+=1;continue
   self.count(family,"candidate_emitted");self.count(family,"unique_economic_opportunity_count")
   self.last_candidate_ms=now
   decision_books={leg["token_id"]:self.books[leg["token_id"]] for leg in relation["legs"]}
   evidence={"schema":"polymarket_v7_unified_exact_arb_graph_opportunity_v1",**SAFETY,
     "model_sha":self.a.model_sha,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
     "graph_generation":self.generation,"relation_id":relation["relation_id"],"relation":relation,
     "opportunity_id":opportunity_id,"trigger_event_id":event_id,"timestamp_ms":now,
     "decision_books":decision_books,"decision_books_sha256":sha(decision_books),
     "counterfactual_modes":["SEQUENTIAL","PARALLEL","BATCH"],"capital_conflict":False,
     "inventory_reserved":r["direction"]=="SELL","resource_vector":required,
     "execution_candidate":native_binary_candidate(relation,r,now),"result":r}
   if opportunity_id not in self.emitted:
    self.a.opportunities.parent.mkdir(parents=True,exist_ok=True)
    with self.a.opportunities.open("a") as f:f.write(json.dumps(evidence,sort_keys=True)+"\n")
    self.emitted.add(opportunity_id)
   for arm in (1,2,5,10,25,50,100):self.pending.append((now+arm,relation,r["direction"],r["quantity"],arm,self.generation))

 def status(self):
  now=time.time_ns()//1000000
  near={k:quantiles(v) for k,v in self.dist.items()}
  for key,value in self.distance_min.items():near[key]["min"]=float(value)
  for key,values in self.dist.items():
   if key.endswith("_ticks") and values:
    for threshold in (.25,.5,1,2,5):
     near[key]["fraction_within_"+str(threshold)+"_tick"]=sum(0<=v<=threshold for v in values)/len(values)
  return {"schema":SCHEMA,"model_sha":self.a.model_sha,**SAFETY,
    "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","state":self.graph_state,"graph_generation":self.generation,
    "graph_generation_compiled_at_ms":self.generation_compiled_at_ms,"nodes":len(self.graph_metadata.get("nodes",[])),
    "unverified_candidates":len(self.graph_metadata.get("unverified_candidates",[])),
    "relations_compiled":len(self.relations),"relations_evaluated":self.funnel["relations_considered"],
    "relations_by_family":dict(Counter(r["relation_family"] for r in self.relations)),
    "capital_limit_pusd":self.capital(),"resources_reserved":{k:str(v) for k,v in self.resources.used().items()},
    "funnel":dict(self.funnel),"funnel_by_family":{k:dict(v) for k,v in self.funnel_by_family.items()},
    "rejection_reasons":dict(self.rejects),"rejection_reasons_by_family":{k:dict(v) for k,v in self.rejects_by_family.items()},
    "near_arbitrage":near,"distance_sampling":"MIN_ALL_OBSERVATIONS; QUANTILES_AND_FRACTIONS_LAST_100000_PER_METRIC",
    "survival_evidence":list(self.survival_evidence),
    "opportunity_lifetime_ms":{k:quantiles(v) for k,v in self.lifetimes.items()},
    "maker_bridge":{family:{"quote_improvement_needed_pusd":max(0.0,summary.get("min",0.0)),
        "paired_fill_probability":None,"one_leg_fill_risk":None,"time_to_second_leg_ms":None,
        "expected_unwind_cost":None,"state":"NEEDS_JOINT_PASSIVE_FILL_OBSERVATIONS",
        "execution_authority":False} for key,summary in near.items()
        for family,separator,stage in [key.partition(":")] if stage=="distance_to_after_reserve_arbitrage"},
    "lifetime_semantics":"LAST_CAUSAL_EXECUTABLE_MINUS_FIRST; OPEN_EPISODES_RIGHT_CENSORED",
    "open_opportunity_episodes":len(self.active),"evaluation_latency_us":quantiles(self.latency_us),
    "events_processed":self.events_processed,"last_candidate_timestamp_ms":self.last_candidate_ms,
    "book_lag_ms":now-self.last_event_ms if self.last_event_ms else None,
    "worker_health":"RUNNING","dropped_observations":dict(self.dropped),"timestamp_ms":now,
    "recovery_policy":"DETERMINISTIC_TAPE_REPLAY_WITH_DURABLE_OPPORTUNITY_IDS"}

 def run(self):
  anchors=JsonlCursor(self.a.tape)
  deltas=JsonlCursor(self.a.delta_tape,segmented=True) if getattr(self.a,"delta_tape",None) else None
  while True:
   self.graph(as_of_ms=time.time_ns()//1_000_000)
   if self.graph_state=="COLLECTING":
    try:
     for row in anchors.poll():
      if deltas is not None:self.reconstruction.anchor(row)
      else:self.update(row)
     if deltas is not None:
      for row in deltas.poll():
       now,books=self.reconstruction.delta(row)
       self.update_decoded(now,books,sha(row))
    except (OSError,ValueError,KeyError,TypeError):
     self.dropped["tape_invalid"]+=1;self.graph_state="BLOCKED_TAPE_INVALID"
   atomic(self.a.status,self.status())
   time.sleep(max(.001,self.a.interval_ms/1000))


def main():
 p=argparse.ArgumentParser()
 for name in ("graph","tape","status","opportunities","capital-policy"):p.add_argument("--"+name,type=Path,required=True)
 p.add_argument("--delta-tape",type=Path)
 p.add_argument("--resource-snapshot",type=Path)
 p.add_argument("--model-sha",required=True);p.add_argument("--interval-ms",type=int,default=10);a=p.parse_args()
 if len(a.model_sha)!=40 or not 1<=a.interval_ms<=1000:raise SystemExit("invalid arguments")
 Shadow(a).run()


if __name__=="__main__":main()
