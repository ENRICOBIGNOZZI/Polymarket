#!/usr/bin/env python3
"""Exchange-native zero-authority PAPER execution shadow for pure arbitrage.

Models the production CLOB V2 mechanics relevant to multi-leg arbitrage:
- per-market mandatory taker delay from immutable market execution terms;
- fresh revalidation after the delay;
- two independent marketable-limit FOK legs with bounded inter-leg skew;
- one-leg failure followed by a causal immediate unwind;
- fee calculation at match time in USDC-value terms;
- fail-closed venue mode and semantic-fingerprint reset.

No authenticated API. No real orders. No automatic promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_pm_repricing_common import atomic_json

CYCLE_SCHEMAS={
    "polymarket_v7_pure_arb_paper_cycle_v2",
    "polymarket_v7_pure_arb_paper_cycle_v3",
}
ROW_SCHEMA="polymarket_v7_pure_arb_exchange_execution_cycle_v1"
STATUS_SCHEMA="polymarket_v7_pure_arb_exchange_execution_status_v1"
VENUE_SCHEMA="polymarket_v7_pure_arb_venue_mode_v1"
TERMS_SCHEMA="polymarket_v7_market_execution_terms_v1"
SELECTION_SCHEMA="polymarket_v7_multi_crypto_book_selection_v1"
SEMANTICS_SCHEMA="polymarket_v7_exchange_semantics_v1"


def load(path:Path|None)->dict[str,Any]:
    if path is None:return {}
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}


def fee_per_share(price:float,rate:float,exponent:float)->float:
    if not(math.isfinite(price) and 0<price<1 and math.isfinite(rate)
           and 0<=rate<=1 and math.isfinite(exponent) and exponent>=0):
        return math.nan
    return rate*(price*(1-price))**exponent if rate else 0.0


def fee_params(market:dict[str,Any])->tuple[float,float]|None:
    fs=market.get("fee_schedule")
    if isinstance(fs,dict):
        try:r=float(fs["rate"]);e=float(fs.get("exponent",1.0))
        except (KeyError,TypeError,ValueError):return None
        if math.isfinite(r) and 0<=r<=1 and math.isfinite(e) and e>=0:return r,e
    if market.get("fees_enabled_explicit") is True and market.get("fees_enabled") is False:
        return 0.0,1.0
    return None


def selection_map(v:dict[str,Any],sha:str)->dict[str,dict[str,Any]]:
    if (v.get("schema")!=SELECTION_SCHEMA or v.get("model_sha")!=sha
        or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False
        or v.get("execution_authority") is not False):
        return {}
    out={}
    for row in v.get("markets") or []:
        if not isinstance(row,dict):continue
        mid=str(row.get("market_id") or "")
        if mid:out[mid]=row
    return out


def fingerprint(market:dict[str,Any])->str:
    payload={
        "market_id":str(market.get("market_id") or ""),
        "event_id":str(market.get("event_id") or ""),
        "yes_token":str(market.get("yes_token") or ""),
        "no_token":str(market.get("no_token") or ""),
        "start_timestamp_ms":int(market.get("start_timestamp_ms") or 0),
        "end_timestamp_ms":int(market.get("end_timestamp_ms") or 0),
        "normalized_rules_hash":str(market.get("normalized_rules_hash") or ""),
        "rule_snapshot_sha256":str(market.get("rule_snapshot_sha256") or ""),
    }
    body=json.dumps(payload,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(body.encode()).hexdigest()


def market_terms(root:Path,market_id:str)->dict[str,Any]:
    v=load(root/(market_id+".json"))
    if (v.get("schema")!=TERMS_SCHEMA or v.get("market_id")!=market_id
        or v.get("state")!="VERIFIED_SNAPSHOT" or v.get("paper_only") is not True):
        return {}
    try:delay=int(v.get("mandatory_taker_delay_ns"))
    except (TypeError,ValueError):return {}
    if delay<0 or delay>5_000_000_000:return {}
    return {**v,"mandatory_taker_delay_ns":delay}


def validate_semantics(v:dict[str,Any])->bool:
    fee=v.get("fee_semantics") if isinstance(v.get("fee_semantics"),dict) else {}
    safety=v.get("safety") if isinstance(v.get("safety"),dict) else {}
    supported=fee.get("supported_buy_collection_modes")
    mode=str(fee.get("buy_collection_mode") or "")
    return (
        v.get("schema")==SEMANTICS_SCHEMA
        and v.get("production_clob_version")=="V2"
        and v.get("collateral_asset")=="pUSD"
        and fee.get("calculation_time")=="MATCH_TIME"
        and fee.get("settlement_asset")=="USDC_VALUE"
        and isinstance(supported,list)
        and set(str(x) for x in supported)=={"USDC_VALUE","SHARES_ON_BUY"}
        and mode in {"USDC_VALUE","SHARES_ON_BUY"}
        and float(fee.get("maker_fee_rate",math.nan))==0.0
        and safety.get("unverified_buy_collection_mode_policy")=="NON_EXECUTABLE_TAKER"
        and safety.get("paper_only") is True
        and safety.get("authenticated_execution") is False
        and safety.get("real_order_submission") is False
    )


def buy_pair_edge_per_net_share(
    yes_price:float,no_price:float,rate:float,exponent:float,mode:str
)->float:
    fy=fee_per_share(yes_price,rate,exponent)
    fn=fee_per_share(no_price,rate,exponent)
    if not(math.isfinite(fy) and math.isfinite(fn)):return math.nan
    if mode=="USDC_VALUE":
        return 1.0-yes_price-no_price-fy-fn
    if mode=="SHARES_ON_BUY":
        yes_net_factor=1.0-fy/yes_price
        no_net_factor=1.0-fn/no_price
        if yes_net_factor<=0 or no_net_factor<=0:return math.nan
        return 1.0-yes_price/yes_net_factor-no_price/no_net_factor
    return math.nan


def gross_buy_quantity_for_net(
    net_quantity:float,price:float,rate:float,exponent:float,mode:str
)->float:
    if not(math.isfinite(net_quantity) and net_quantity>0):return math.nan
    f=fee_per_share(price,rate,exponent)
    if not math.isfinite(f):return math.nan
    if mode=="USDC_VALUE":return net_quantity
    if mode=="SHARES_ON_BUY":
        factor=1.0-f/price
        return net_quantity/factor if factor>0 else math.nan
    return math.nan


def venue_policy(v:dict[str,Any], field:str="simulation_policy")->dict[str,bool]:
    if (v.get("schema")!=VENUE_SCHEMA or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False):
        return {"new_taker":False,"new_maker":False,"cancel":True}
    if field not in {"observed_policy","simulation_policy"}:
        return {"new_taker":False,"new_maker":False,"cancel":True}
    p=v.get(field)
    if not isinstance(p,dict):return {"new_taker":False,"new_maker":False,"cancel":True}
    return {k:p.get(k) is True for k in ("new_taker","new_maker","cancel")}


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
                try:r=json.loads(raw)
                except (ValueError,UnicodeDecodeError):continue
                if (isinstance(r,dict) and r.get("schema") in CYCLE_SCHEMAS
                    and r.get("model_sha")==self.sha and r.get("paper_only") is True
                    and r.get("authenticated_execution") is False
                    and r.get("real_order_submission") is False):
                    out.append(r)
            try:
                old,cur=os.fstat(self.handle.fileno()),self.path.stat()
                if (old.st_dev,old.st_ino)==(cur.st_dev,cur.st_ino):
                    if cur.st_size<self.handle.tell():self.handle.seek(0)
                    return out
            except OSError:return out
            self.handle.close();self.handle=None
        return out


def book_point(book:BookTimeline,mid:str,token:str,at_ms:int,side:str)->dict[str,float]|None:
    row=book.asof(mid,token,at_ms)
    if row is None:return None
    try:
        ts=int(row["receive_wall_ms"])
        if side=="BUY":
            price=float(row["best_ask"]);depth=float(row.get("ask_depth_l1") or 0.0)
            unwind=float(row["best_bid"]);unwind_depth=float(row.get("bid_depth_l1") or 0.0)
        else:
            price=float(row["best_bid"]);depth=float(row.get("bid_depth_l1") or 0.0)
            unwind=float(row["best_ask"]);unwind_depth=float(row.get("ask_depth_l1") or 0.0)
    except (KeyError,TypeError,ValueError,OverflowError):
        return None
    if not(0<price<1 and depth>=0 and 0<unwind<1 and unwind_depth>=0):return None
    return {"ts":ts,"price":price,"depth":depth,"unwind":unwind,"unwind_depth":unwind_depth}


def fok_fill(point:dict[str,float]|None,*,side:str,limit:float,quantity:float,
             target_ms:int,maximum_book_age_ms:int)->bool:
    if point is None or quantity<=0:return False
    age=target_ms-int(point["ts"])
    if age<0 or age>maximum_book_age_ms:return False
    price=float(point["price"])
    price_ok=price<=limit+1e-12 if side=="BUY" else price>=limit-1e-12
    return price_ok and float(point["depth"])+1e-12>=quantity


def entry_pnl(side:str,price:float,quantity:float,fee_rate:float,fee_exp:float,
              buy_collection_mode:str="USDC_VALUE")->float:
    fee=fee_per_share(price,fee_rate,fee_exp)
    if not math.isfinite(fee):return math.nan
    if side=="SELL":return (price-fee)*quantity
    if buy_collection_mode=="USDC_VALUE":return (-price-fee)*quantity
    if buy_collection_mode=="SHARES_ON_BUY":return -price*quantity
    return math.nan


def unwind_pnl(side:str,price:float,quantity:float,fee_rate:float,fee_exp:float)->float:
    fee=fee_per_share(price,fee_rate,fee_exp)
    if not math.isfinite(fee):return math.nan
    # Reverse the first leg: BUY entry -> SELL unwind; SELL entry -> BUY unwind.
    return (price-fee)*quantity if side=="BUY" else (-price-fee)*quantity


class Shadow:
    def __init__(self,args:argparse.Namespace):
        self.args=args
        self.tail=Tail(args.candidates,args.model_sha)
        self.book=BookTimeline(
            args.book_tape,args.model_sha,
            retention_ms=max(10_000,max(args.inter_leg_skew_ms)+args.maximum_book_age_ms
                             +args.unwind_delay_ms+5000))
        self.rows=[];self.seen=set();self.pending=[]
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.status.parent.mkdir(parents=True,exist_ok=True)
        self.semantics=load(args.exchange_semantics)
        if not validate_semantics(self.semantics):
            raise ValueError("exchange semantics invalid")
        self._restore()

    def _restore(self):
        try:
            for raw in self.args.output.read_text(encoding="utf-8").splitlines():
                r=json.loads(raw)
                if r.get("schema")==ROW_SCHEMA and r.get("model_sha")==self.args.model_sha:
                    self.rows.append(r);self.seen.add(str(r.get("scenario_id") or ""))
        except (OSError,json.JSONDecodeError):pass

    def ingest(self):
        selection=selection_map(load(self.args.selection),self.args.model_sha)
        venue=load(self.args.venue_mode)
        observed_can_taker=venue_policy(venue,"observed_policy")["new_taker"]
        simulation_can_taker=venue_policy(venue,"simulation_policy")["new_taker"]
        observed_mode=str(venue.get("observed_mode") or "DEGRADED")
        simulation_mode=str(venue.get("simulation_mode") or "DEGRADED")
        counterfactual=venue.get("paper_counterfactual") is True
        for c in self.tail.poll():
            mid=str(c.get("market_id") or "");kind=str(c.get("kind") or "")
            try:detected=int(c.get("receive_wall_ms") or 0)
            except (TypeError,ValueError):continue
            market=selection.get(mid)
            if detected<=0 or market is None or kind not in {"BUY_COMPLETE_SET","SELL_COMPLETE_SET"}:
                continue
            terms=market_terms(self.args.market_terms_root,mid)
            base_id=f"{mid}:{kind}:{detected}"
            for transport in self.args.transport_delay_ms:
                for skew in self.args.inter_leg_skew_ms:
                    for order in ("YES_FIRST","NO_FIRST"):
                        sid=f"{base_id}:{transport}:{skew}:{order}"
                        if sid in self.seen:continue
                        mandatory=(int(terms["mandatory_taker_delay_ns"])//1_000_000) if terms else None
                        total_delay=(mandatory+transport) if mandatory is not None else None
                        self.pending.append({
                            "scenario_id":sid,"candidate":c,"market":market,
                            "fingerprint":fingerprint(market),"terms":terms,
                            "observed_venue_taker_allowed":observed_can_taker,
                            "simulation_taker_allowed":simulation_can_taker,
                            "observed_venue_mode":observed_mode,
                            "simulation_venue_mode":simulation_mode,
                            "paper_counterfactual":counterfactual,
                            "transport_ms":transport,"skew_ms":skew,"order":order,
                            "total_delay_ms":total_delay,
                            "target_ms":detected+total_delay if total_delay is not None else None,
                        })
                        self.seen.add(sid)

    def evaluate(self,item:dict[str,Any])->dict[str,Any]:
        c=item["candidate"];m=item["market"];mid=str(c.get("market_id"))
        kind=str(c.get("kind"));buy=kind=="BUY_COMPLETE_SET"
        side="BUY" if buy else "SELL";q=max(0.0,float(
            c.get("executable_shares_local_deep")
            or c.get("executable_shares_l10")
            or c.get("executable_shares_l1") or 0.0))
        q=min(q,self.args.maximum_shares)
        base={
            "schema":ROW_SCHEMA,"model_sha":self.args.model_sha,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW",
            "scenario_id":item["scenario_id"],"market_id":mid,
            "asset":str(c.get("asset") or ""),"horizon":str(c.get("horizon") or ""),
            "kind":kind,"transport_delay_ms":item["transport_ms"],
            "market_end_ms":int(m.get("end_timestamp_ms") or 0),
            "mandatory_taker_delay_ns":item["terms"].get("mandatory_taker_delay_ns") if item["terms"] else None,
            "inter_leg_skew_ms":item["skew_ms"],"leg_order":item["order"],
            "target_shares":q,"state":"CENSORED",
            "lifecycle":["CREATED"],"semantic_fingerprint":item["fingerprint"],
            "observed_venue_mode":item.get("observed_venue_mode","DEGRADED"),
            "simulation_venue_mode":item.get("simulation_venue_mode","DEGRADED"),
            "observed_venue_taker_allowed":item.get("observed_venue_taker_allowed") is True,
            "simulation_taker_allowed":item.get("simulation_taker_allowed") is True,
            "paper_counterfactual":item.get("paper_counterfactual") is True,
            "allocation_eligible":False,
        }
        if not item.get("simulation_taker_allowed"):
            base["state"]="BLOCKED_VENUE_MODE";return base
        if not item["terms"]:
            base["state"]="CENSORED_TERMS_UNVERIFIED";return base
        current=selection_map(load(self.args.selection),self.args.model_sha).get(mid)
        if current is None or fingerprint(current)!=item["fingerprint"]:
            base["state"]="SEMANTIC_RESET";base["lifecycle"].append("SEMANTIC_RESET");return base
        fp=fee_params(current)
        if fp is None:
            base["state"]="CENSORED_FEE_UNVERIFIED";return base
        rate,exp=fp
        fee_semantics=self.semantics.get("fee_semantics") or {}
        buy_collection_mode=str(fee_semantics.get("buy_collection_mode") or "")
        if buy_collection_mode not in {"USDC_VALUE","SHARES_ON_BUY"}:
            base["state"]="CENSORED_BUY_FEE_COLLECTION_UNVERIFIED";return base
        yes,no=str(current.get("yes_token") or ""),str(current.get("no_token") or "")
        target=int(item["target_ms"]);skew=int(item["skew_ms"])
        if target<=0 or q<self.args.minimum_shares:
            base["state"]="NO_TRADE_SIZE";return base

        base["lifecycle"]+=["SENT","ACK_PENDING","PENDING_DELAY"]
        y0=book_point(self.book,mid,yes,target,side)
        n0=book_point(self.book,mid,no,target,side)
        if y0 is None or n0 is None:
            base["state"]="CENSORED_ARRIVAL_BOOK";return base
        if max(target-int(y0["ts"]),target-int(n0["ts"]))>self.args.maximum_book_age_ms:
            base["state"]="CENSORED_STALE_ARRIVAL";return base
        if abs(int(y0["ts"])-int(n0["ts"]))>self.args.maximum_leg_skew_ms:
            base["state"]="CENSORED_BOOK_SKEW";return base

        fees=fee_per_share(y0["price"],rate,exp)+fee_per_share(n0["price"],rate,exp)
        raw=(1-y0["price"]-n0["price"]) if buy else (y0["price"]+n0["price"]-1)
        if buy:
            configured_before_reserve=buy_pair_edge_per_net_share(
                y0["price"],n0["price"],rate,exp,buy_collection_mode)
            sensitivity={
                mode:buy_pair_edge_per_net_share(y0["price"],n0["price"],rate,exp,mode)
                for mode in ("USDC_VALUE","SHARES_ON_BUY")
            }
            edge=configured_before_reserve-self.args.reserve_per_share
        else:
            configured_before_reserve=raw-fees
            sensitivity={}
            edge=configured_before_reserve-self.args.reserve_per_share
        base.update({
            "revalidation_wall_ms":target,"revalidation_yes_price":y0["price"],
            "revalidation_no_price":n0["price"],"revalidation_raw_edge":raw,
            "revalidation_fee_per_share":fees,
            "buy_fee_collection_mode":buy_collection_mode if buy else None,
            "buy_fee_collection_edge_sensitivity":sensitivity,
            "revalidation_edge_before_reserve":configured_before_reserve,
            "revalidation_edge_after_reserve":edge,
        })
        if not math.isfinite(fees) or edge<=0:
            base["state"]="REVALIDATION_REJECTED"
            base["lifecycle"].append("REVALIDATION_REJECTED");return base
        base["lifecycle"].append("ARRIVAL_REVALIDATED")

        if buy:
            leg_quantities={
                "YES":gross_buy_quantity_for_net(
                    q,y0["price"],rate,exp,buy_collection_mode),
                "NO":gross_buy_quantity_for_net(
                    q,n0["price"],rate,exp,buy_collection_mode),
            }
            if not all(math.isfinite(x) and x>0 for x in leg_quantities.values()):
                base["state"]="CENSORED_BUY_FEE_COLLECTION_INVALID";return base
        else:
            leg_quantities={"YES":q,"NO":q}
        base["leg_gross_quantities"]=leg_quantities
        base["target_net_paired_shares"]=q

        limits={"YES":y0["price"],"NO":n0["price"]}
        times={
            item["order"].split("_")[0]:target,
            ("NO" if item["order"].startswith("YES") else "YES"):target+skew,
        }
        fills={}
        entry_cash=0.0
        for leg in (item["order"].split("_")[0],
                    "NO" if item["order"].startswith("YES") else "YES"):
            token=yes if leg=="YES" else no
            point=book_point(self.book,mid,token,times[leg],side)
            gross_quantity=leg_quantities[leg]
            ok=fok_fill(point,side=side,limit=limits[leg],quantity=gross_quantity,
                        target_ms=times[leg],maximum_book_age_ms=self.args.maximum_book_age_ms)
            fills[leg]={"filled":ok,"wall_ms":times[leg],
                        "gross_quantity":gross_quantity,
                        "net_position_shares":q if ok else 0.0,
                        "price":point["price"] if point else None}
            base["lifecycle"].append(f"{leg}_{'FILLED' if ok else 'REJECTED'}")
            if ok and point is not None:
                leg_pnl=entry_pnl(
                    side,point["price"],gross_quantity,rate,exp,buy_collection_mode)
                entry_cash+=leg_pnl

        filled=[leg for leg,v in fills.items() if v["filled"]]
        base["legs"]=fills
        if len(filled)==2:
            redemption=q if buy else -q
            pnl=entry_cash+redemption
            base.update(state="COMPLETE_PAIRED",paired_execution=True,
                        entry_cashflow=entry_cash,redemption_cashflow=redemption,
                        execution_pnl_pre_reserve=pnl,
                        execution_pnl_after_reserve=pnl-q*self.args.reserve_per_share,
                        unwind_pnl=0.0,
                        allocation_eligible=bool(item.get("observed_venue_taker_allowed")))
            base["lifecycle"].append("COMPLETE")
            return base
        if len(filled)==0:
            base["state"]="BOTH_REJECTED";base["paired_execution"]=False
            base["lifecycle"].append("REJECTED");return base

        first=filled[0];token=yes if first=="YES" else no
        unwind_at=max(times.values())+self.args.unwind_delay_ms
        point=book_point(self.book,mid,token,unwind_at,side)
        if point is None:
            base["state"]="ONE_LEG_UNWIND_CENSORED";base["paired_execution"]=False
            base["lifecycle"].append("UNWIND_CENSORED");return base
        unwind_price=float(point["unwind"])
        unwind_depth=float(point["unwind_depth"])
        if unwind_depth+1e-12<q:
            base["state"]="ONE_LEG_UNWIND_DEPTH_FAILURE";base["paired_execution"]=False
            base["lifecycle"].append("UNWIND_DEPTH_FAILURE");return base
        u=unwind_pnl(side,unwind_price,q,rate,exp)
        total=entry_cash+u
        base.update(
            state="ONE_LEG_UNWOUND",paired_execution=False,
            first_filled_leg=first,unwind_wall_ms=unwind_at,
            unwind_price=unwind_price,entry_cashflow=entry_cash,
            unwind_pnl=u,execution_pnl_pre_reserve=total,
            execution_pnl_after_reserve=total-q*self.args.reserve_per_share,
            allocation_eligible=bool(item.get("observed_venue_taker_allowed")))
        base["lifecycle"]+=["UNWIND_SENT","UNWIND_FILLED","COMPLETE_WITH_LEGGING"]
        return base

    def evaluate_ready(self):
        remain=[]
        for item in self.pending:
            target=item.get("target_ms")
            if target is None:
                row=self.evaluate(item)
            else:
                needed=int(target)+int(item["skew_ms"])+self.args.unwind_delay_ms
                if self.book.watermark_ms<needed:
                    remain.append(item);continue
                row=self.evaluate(item)
            with self.args.output.open("a",encoding="utf-8") as h:
                h.write(json.dumps(row,sort_keys=True)+"\n")
            self.rows.append(row)
        self.pending=remain

    def publish(self):
        states=Counter(str(r.get("state")) for r in self.rows)
        by_delay=defaultdict(list);by_skew=defaultdict(list)
        for r in self.rows:
            by_delay[str(r.get("mandatory_taker_delay_ns"))].append(r)
            by_skew[str(r.get("inter_leg_skew_ms"))].append(r)
        def summary(rows):
            pnl=[float(r["execution_pnl_after_reserve"]) for r in rows
                 if isinstance(r.get("execution_pnl_after_reserve"),(int,float))]
            return {
                "scenarios":len(rows),
                "paired":sum(r.get("state")=="COMPLETE_PAIRED" for r in rows),
                "one_leg_unwound":sum(r.get("state")=="ONE_LEG_UNWOUND" for r in rows),
                "mean_pnl_after_reserve":sum(pnl)/len(pnl) if pnl else None,
                "sum_pnl_after_reserve":sum(pnl) if pnl else 0.0,
                "states":dict(Counter(str(r.get("state")) for r in rows)),
            }
        observed_admitted=[
            r for r in self.rows
            if r.get("observed_venue_taker_allowed") is True
            and r.get("allocation_eligible") is True
        ]
        counterfactual_only=[
            r for r in self.rows
            if r.get("paper_counterfactual") is True
            and r.get("simulation_taker_allowed") is True
            and r.get("observed_venue_taker_allowed") is not True
        ]
        atomic_json(self.args.status,{
            "schema":STATUS_SCHEMA,"model_sha":self.args.model_sha,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW",
            "state":"COLLECTING","timestamp_ms":time.time_ns()//1_000_000,
            "pending":len(self.pending),"evaluated":len(self.rows),"states":dict(states),
            "observed_admitted_scenarios":len(observed_admitted),
            "counterfactual_only_scenarios":len(counterfactual_only),
            "counterfactual_only_pnl_after_reserve":sum(
                float(r.get("execution_pnl_after_reserve") or 0.0)
                for r in counterfactual_only),
            "by_mandatory_delay_ns":{k:summary(v) for k,v in sorted(by_delay.items())},
            "by_inter_leg_skew_ms":{k:summary(v) for k,v in sorted(by_skew.items(),key=lambda x:int(x[0]))},
            "fee_semantics":"CLOB_V2_MATCH_TIME_USDC_VALUE",
            "multileg_semantics":"INDEPENDENT_MARKETABLE_LIMIT_FOK_WITH_CAUSAL_UNWIND",
            "unknown_terms_policy":"FAIL_CLOSED",
        })

    def run(self):
        while True:
            self.book.poll();self.ingest();self.evaluate_ready();self.publish()
            time.sleep(self.args.interval_ms/1000.0)


def parse_ints(value:str)->list[int]:
    return sorted({int(x) for x in value.split(",") if int(x)>=0})


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates",type=Path,required=True)
    ap.add_argument("--book-tape",type=Path,required=True)
    ap.add_argument("--selection",type=Path,required=True)
    ap.add_argument("--market-terms-root",type=Path,required=True)
    ap.add_argument("--venue-mode",type=Path,required=True)
    ap.add_argument("--exchange-semantics",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--transport-delay-ms",default="1,2,5,10")
    ap.add_argument("--inter-leg-skew-ms",default="0,1,2,5,10")
    ap.add_argument("--unwind-delay-ms",type=int,default=2)
    ap.add_argument("--maximum-book-age-ms",type=int,default=100)
    ap.add_argument("--maximum-leg-skew-ms",type=int,default=100)
    ap.add_argument("--reserve-per-share",type=float,default=.0005)
    ap.add_argument("--minimum-shares",type=float,default=1.0)
    ap.add_argument("--maximum-shares",type=float,default=1000.0)
    ap.add_argument("--interval-ms",type=int,default=5)
    args=ap.parse_args()
    args.transport_delay_ms=parse_ints(args.transport_delay_ms)
    args.inter_leg_skew_ms=parse_ints(args.inter_leg_skew_ms)
    if len(args.model_sha)!=40 or any(c not in "0123456789abcdef" for c in args.model_sha):
        raise SystemExit("invalid sha")
    if not(args.transport_delay_ms and args.inter_leg_skew_ms
           and 0<=args.unwind_delay_ms<=5000 and 1<=args.maximum_book_age_ms<=5000
           and 0<=args.maximum_leg_skew_ms<=5000 and 0<=args.reserve_per_share<1
           and 0<args.minimum_shares<=args.maximum_shares and 1<=args.interval_ms<=1000):
        raise SystemExit("invalid arguments")
    Shadow(args).run();return 0


if __name__=="__main__":
    raise SystemExit(main())
