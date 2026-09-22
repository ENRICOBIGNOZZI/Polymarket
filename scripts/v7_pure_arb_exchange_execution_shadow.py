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
from collections import Counter, defaultdict, deque
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_pm_repricing_common import atomic_json
from v7_pure_arb_economics import (
    TAKER_REBATE_TIERS,
    fok_sweep,
    parse_levels,
    raw_fee_per_share,
    sweep_fee_usdc,
    tail_jsonl,
    weighted_volume,
)

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
    return raw_fee_per_share(price,rate,exponent)


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


def venue_policy(v:dict[str,Any])->dict[str,bool]:
    if (v.get("schema")!=VENUE_SCHEMA or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False):
        return {"new_taker":False,"new_maker":False,"cancel":True}
    p=v.get("simulation_policy")
    if not isinstance(p,dict):return {"new_taker":False,"new_maker":False,"cancel":True}
    return {k:p.get(k) is True for k in ("new_taker","new_maker","cancel")}


def verified_taker_rebate(path:Path|None,sha:str,now_ms:int)->tuple[bool,float,str|None]:
    v=load(path)
    if (v.get("schema")!="polymarket_v7_fee_reward_registry_v1"
        or v.get("model_sha")!=sha or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False):
        return False,0.0,None
    row=v.get("taker_rebate") if isinstance(v.get("taker_rebate"),dict) else {}
    if row.get("verified") is not True:return False,0.0,None
    try:fraction=float(row.get("rebate_fraction"));expires=int(row.get("expires_at_ms") or 0)
    except (TypeError,ValueError,OverflowError):return False,0.0,None
    if not(0<=fraction<=1 and expires>=now_ms):return False,0.0,None
    return True,fraction,str(row.get("tier") or "") or None


def leg_arrival_times(
    mode:str,order:str,target_ms:int,transport_ms:int,skew_ms:int
)->tuple[dict[str,int],tuple[str,str]]:
    mode=str(mode or "SEQUENTIAL").upper()
    if mode=="BATCH":
        return {"YES":target_ms,"NO":target_ms},("YES","NO")
    first=str(order or "YES_FIRST").split("_")[0]
    if first not in {"YES","NO"}:first="YES"
    second="NO" if first=="YES" else "YES"
    extra=max(0,int(skew_ms))
    if mode=="SEQUENTIAL":
        extra+=max(0,int(transport_ms))
    elif mode!="PARALLEL":
        raise ValueError("unsupported execution mode")
    return {first:int(target_ms),second:int(target_ms)+extra},(first,second)


class Tail:
    def __init__(self,path:Path,sha:str,*,start_at_end:bool=False):
        self.path,self.sha,self.handle=path,sha,None
        self.start_at_end_pending=bool(start_at_end)
    def poll(self)->list[dict[str,Any]]:
        out=[]
        for _ in range(2):
            if self.handle is None:
                try:self.handle=self.path.open("rb")
                except OSError:return out
                if self.start_at_end_pending:
                    self.handle.seek(0,os.SEEK_END)
                    self.start_at_end_pending=False
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


DEEP_BOOK_SCHEMA="polymarket_v7_pure_arb_deep_book_snapshot_v1"


class DeepReplayTimeline:
    """Candidate-time deep snapshot + canonical incremental book mutations."""

    def __init__(self,path:Path,sha:str):
        self.path,self.sha,self.handle=path,sha,None
        self.snapshots=defaultdict(lambda:deque(maxlen=256))

    @staticmethod
    def _levels(raw:Any)->dict[int,float]:
        out={}
        if not isinstance(raw,list):return out
        for level in raw:
            if not isinstance(level,dict):continue
            try:
                price=int(round(float(level.get("price"))*10_000))
                size=float(level.get("size"))
            except (TypeError,ValueError,OverflowError):continue
            if 0<price<10_000 and math.isfinite(size) and size>0:
                out[price]=size
        return out

    def ingest(self,row:Any)->None:
        if (not isinstance(row,dict) or row.get("schema")!=DEEP_BOOK_SCHEMA
            or row.get("model_sha")!=self.sha or row.get("paper_only") is not True
            or row.get("authenticated_execution") is not False
            or row.get("real_order_submission") is not False
            or row.get("execution_authority")!="ZERO_AUTHORITY_RESEARCH_ONLY"):
            return
        try:
            mid=str(row["market_id"]);ts=int(row["receive_wall_ms"])
            epoch=int(row["connection_epoch"]);session=str(row["observer_session_id"])
            yes=str(row["yes_token"]);no=str(row["no_token"])
        except (KeyError,TypeError,ValueError,OverflowError):return
        if not(mid and yes and no and session and ts>0 and epoch>0):return
        if any(row.get(k) is True for k in (
            "yes_bid_truncated","yes_ask_truncated","no_bid_truncated","no_ask_truncated")):
            return
        try:origin=int(row.get("capture_origin_wall_ms") or ts)
        except (TypeError,ValueError,OverflowError):origin=ts
        snap={
            "market_id":mid,"receive_wall_ms":ts,"capture_origin_wall_ms":origin,
            "connection_epoch":epoch,
            "observer_session_id":session,"yes_token":yes,"no_token":no,
            "yes_bids":self._levels(row.get("yes_bid_levels")),
            "yes_asks":self._levels(row.get("yes_ask_levels")),
            "no_bids":self._levels(row.get("no_bid_levels")),
            "no_asks":self._levels(row.get("no_ask_levels")),
        }
        if all(snap[k] for k in ("yes_bids","yes_asks","no_bids","no_asks")):
            self.snapshots[mid].append(snap)

    def poll(self)->None:
        for _ in range(2):
            if self.handle is None:
                try:self.handle=self.path.open("rb")
                except OSError:return
            while True:
                pos=self.handle.tell();raw=self.handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    self.handle.seek(pos);break
                try:self.ingest(json.loads(raw))
                except (ValueError,UnicodeDecodeError):continue
            try:
                old,cur=os.fstat(self.handle.fileno()),self.path.stat()
                if (old.st_dev,old.st_ino)==(cur.st_dev,cur.st_ino):
                    if cur.st_size<self.handle.tell():self.handle.seek(0)
                    return
            except OSError:return
            self.handle.close();self.handle=None

    def levels_at(self,market:str,token:str,origin_ms:int,target_ms:int,
                  side:str,book:BookTimeline)->list[tuple[float,float]]|None:
        snapshot=None
        # Prefer the newest full deep snapshot belonging to this exact candidate
        # episode and available no later than the queried arrival. This safely
        # re-anchors replay after a full-book replace/tick transition.
        for row in reversed(self.snapshots.get(market,())):
            same_episode=int(row.get("capture_origin_wall_ms") or 0)==origin_ms
            if (same_episode and row["receive_wall_ms"]<=target_ms
                and token in {row["yes_token"],row["no_token"]}):
                snapshot=row;break
        if snapshot is None:
            # Backward-compatible exact-origin snapshot for older evidence.
            for row in reversed(self.snapshots.get(market,())):
                if (row["receive_wall_ms"]<=origin_ms
                    and token in {row["yes_token"],row["no_token"]}):
                    snapshot=row;break
        if snapshot is None:return None
        if (snapshot["observer_session_id"]!=book.session
            or snapshot["connection_epoch"]!=book.epoch):
            return None
        prefix="yes" if token==snapshot["yes_token"] else "no"
        bids=dict(snapshot[prefix+"_bids"]);asks=dict(snapshot[prefix+"_asks"])
        start=int(snapshot["receive_wall_ms"])
        for row in book.between(market,token,start,target_ms):
            if (row.get("observer_session_id")!=snapshot["observer_session_id"]
                or int(row.get("connection_epoch") or 0)!=snapshot["connection_epoch"]
                or row.get("valid") is not True or row.get("lineage_continuous") is not True):
                return None
            if row.get("deep_replay_reset") is True:return None
            change=row.get("book_change")
            if not isinstance(change,dict):continue
            try:
                price=int(round(float(change.get("price"))*10_000))
                size=float(change.get("size"));change_side=str(change.get("side") or "").upper()
            except (TypeError,ValueError,OverflowError):return None
            if not(0<price<10_000 and math.isfinite(size) and size>=0):return None
            levels=bids if change_side=="BUY" else asks if change_side=="SELL" else None
            if levels is None:return None
            if size<=0:levels.pop(price,None)
            else:levels[price]=size
        chosen=asks if side.upper()=="BUY" else bids
        ordered=sorted(chosen.items(),reverse=side.upper()=="SELL")
        return [(price/10_000.0,size) for price,size in ordered if size>0]


def book_point(book:BookTimeline,mid:str,token:str,at_ms:int,side:str,
               *,deep:DeepReplayTimeline|None=None,origin_ms:int|None=None)->dict[str,Any]|None:
    row=book.asof(mid,token,at_ms)
    if row is None:return None
    try:ts=int(row["receive_wall_ms"])
    except (KeyError,TypeError,ValueError,OverflowError):return None
    levels=None;unwind_levels=None;source="CAUSAL_L10"
    if deep is not None and origin_ms is not None:
        levels=deep.levels_at(mid,token,origin_ms,at_ms,side,book)
        reverse="SELL" if side=="BUY" else "BUY"
        unwind_levels=deep.levels_at(mid,token,origin_ms,at_ms,reverse,book)
        if levels and unwind_levels:source="CAUSAL_DEEP_REPLAY_1024"
        else:levels=unwind_levels=None
    if levels is None:
        levels=parse_levels(row,side)
        reverse="SELL" if side=="BUY" else "BUY"
        unwind_levels=parse_levels(row,reverse)
    if not levels or not unwind_levels:return None
    return {
        "ts":ts,
        "levels":levels,
        "unwind_levels":unwind_levels,
        "price":levels[0][0],
        "depth":sum(q for _,q in levels),
        "unwind":unwind_levels[0][0],
        "unwind_depth":sum(q for _,q in unwind_levels),
        "causal_depth_levels":1024 if source=="CAUSAL_DEEP_REPLAY_1024" else int(
            row.get("causal_depth_levels") or len(levels)),
        "depth_source":source,
    }


class FillResult(dict):
    def __bool__(self)->bool:
        return self.get("filled") is True


def fok_fill(point:dict[str,Any]|None,*,side:str,limit:float|None,quantity:float,
             target_ms:int,maximum_book_age_ms:int)->FillResult:
    if point is None or quantity<=0:
        return FillResult({"filled":False,"quantity":0.0,"vwap":None,"notional":0.0,
                "worst_price":None,"levels_used":0,"fills":[]})
    age=target_ms-int(point["ts"])
    if age<0 or age>maximum_book_age_ms:
        return FillResult({"filled":False,"quantity":0.0,"vwap":None,"notional":0.0,
                "worst_price":None,"levels_used":0,"fills":[]})
    levels=point.get("levels")
    if not isinstance(levels,list):
        try:levels=[(float(point["price"]),float(point["depth"]))]
        except (KeyError,TypeError,ValueError,OverflowError):levels=[]
    return FillResult(fok_sweep(levels,quantity,side,limit))


def entry_cashflow(side:str,sweep:dict[str,Any],fee_rate:float,fee_exp:float)->tuple[float,float]:
    fee=sweep_fee_usdc(sweep,fee_rate,fee_exp)
    if not math.isfinite(fee):return math.nan,math.nan
    notional=float(sweep.get("notional") or 0.0)
    return ((notional-fee) if side=="SELL" else (-notional-fee)),fee


def unwind_cashflow(original_side:str,sweep:dict[str,Any],fee_rate:float,fee_exp:float)->tuple[float,float]:
    fee=sweep_fee_usdc(sweep,fee_rate,fee_exp)
    if not math.isfinite(fee):return math.nan,math.nan
    notional=float(sweep.get("notional") or 0.0)
    # Reverse of BUY is SELL; reverse of SELL is BUY.
    return ((notional-fee) if original_side=="BUY" else (-notional-fee)),fee


class Shadow:
    def __init__(self,args:argparse.Namespace):
        self.args=args
        resumed=False
        try:resumed=args.output.exists() and args.output.stat().st_size>0
        except OSError:resumed=False
        # On restart, old scenarios are already durable in output. Follow new
        # candidates only; replaying the entire candidate tape would duplicate
        # historical counterfactuals.
        self.tail=Tail(args.candidates,args.model_sha,start_at_end=resumed)
        self.book=BookTimeline(
            args.book_tape,args.model_sha,
            retention_ms=max(10_000,max(args.inter_leg_skew_ms)+args.maximum_book_age_ms
                             +args.unwind_delay_ms+5000))
        self.deep=(DeepReplayTimeline(args.deep_book_snapshots,args.model_sha)
                   if args.deep_book_snapshots is not None else None)
        self.rows=deque(maxlen=args.status_window_scenarios)
        self.seen=set()
        self.seen_order=deque()
        self.pending=[]
        self.resumed_follow_new_only=resumed
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.status.parent.mkdir(parents=True,exist_ok=True)
        self.semantics=load(args.exchange_semantics)
        if not validate_semantics(self.semantics):
            raise ValueError("exchange semantics invalid")
        self._restore()

    def _restore(self):
        for r in tail_jsonl(
            self.args.output,max_rows=self.args.status_window_scenarios,
            max_bytes=self.args.status_restore_bytes):
            if r.get("schema")==ROW_SCHEMA and r.get("model_sha")==self.args.model_sha:
                self.rows.append(r)

    def _mark_candidate_seen(self,candidate_id:str)->bool:
        if not candidate_id or candidate_id in self.seen:
            return False
        while len(self.seen_order)>=self.args.seen_candidate_limit:
            old=self.seen_order.popleft()
            self.seen.discard(old)
        self.seen.add(candidate_id)
        self.seen_order.append(candidate_id)
        return True

    def ingest(self):
        selection=selection_map(load(self.args.selection),self.args.model_sha)
        venue=load(self.args.venue_mode)
        simulation_policy=venue_policy(venue)
        observed_raw=venue.get("observed_policy") if isinstance(venue.get("observed_policy"),dict) else {}
        observed_policy={k:observed_raw.get(k) is True for k in ("new_taker","new_maker","cancel")}
        can_taker=simulation_policy["new_taker"]
        observed_can_taker=observed_policy["new_taker"]
        for c in self.tail.poll():
            mid=str(c.get("market_id") or "");kind=str(c.get("kind") or "")
            try:detected=int(c.get("receive_wall_ms") or 0)
            except (TypeError,ValueError):continue
            market=selection.get(mid)
            if detected<=0 or market is None or kind not in {"BUY_COMPLETE_SET","SELL_COMPLETE_SET"}:
                continue
            terms=market_terms(self.args.market_terms_root,mid)
            base_id=f"{mid}:{kind}:{detected}"
            if not self._mark_candidate_seen(base_id):continue
            for transport in self.args.transport_delay_ms:
                for execution_mode in self.args.transport_modes:
                    skews=(0,) if execution_mode=="BATCH" else self.args.inter_leg_skew_ms
                    orders=("BATCH",) if execution_mode=="BATCH" else ("YES_FIRST","NO_FIRST")
                    for skew in skews:
                        for order in orders:
                            sid=f"{base_id}:{execution_mode}:{transport}:{skew}:{order}"
                            mandatory=(int(terms["mandatory_taker_delay_ns"])//1_000_000) if terms else None
                            total_delay=(mandatory+transport) if mandatory is not None else None
                            self.pending.append({
                                "scenario_id":sid,"candidate":c,"market":market,
                                "fingerprint":fingerprint(market),"terms":terms,
                                "venue_taker_allowed":can_taker,
                                "observed_taker_allowed":observed_can_taker,
                                "observed_mode":str(venue.get("observed_mode") or "DEGRADED"),
                                "observed_account_mode":str(venue.get("observed_account_mode") or "UNKNOWN"),
                                "simulation_mode":str(venue.get("simulation_mode") or ""),
                                "simulation_account_mode":str(venue.get("simulation_account_mode") or ""),
                                "paper_counterfactual":venue.get("paper_counterfactual") is True,
                                "paper_account_counterfactual":venue.get("paper_account_counterfactual") is True,
                                "execution_mode":execution_mode,
                                "transport_ms":transport,"skew_ms":skew,"order":order,
                                "total_delay_ms":total_delay,
                                "target_ms":detected+total_delay if total_delay is not None else None,
                            })

    def evaluate(self,item:dict[str,Any])->dict[str,Any]:
        c=item["candidate"];m=item["market"];mid=str(c.get("market_id"))
        kind=str(c.get("kind"));buy=kind=="BUY_COMPLETE_SET"
        side="BUY" if buy else "SELL"
        requested_q=max(0.0,float(
            c.get("executable_shares_local_deep")
            or c.get("executable_shares_l10")
            or c.get("executable_shares_l1") or 0.0))
        q=min(requested_q,self.args.maximum_shares)
        mode=str(item.get("execution_mode") or "SEQUENTIAL")
        base={
            "schema":ROW_SCHEMA,"model_sha":self.args.model_sha,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW",
            "scenario_id":item["scenario_id"],
            "candidate_id":f"{mid}:{kind}:{int(c.get('receive_wall_ms') or 0)}",
            "market_id":mid,
            "asset":str(c.get("asset") or ""),"horizon":str(c.get("horizon") or ""),
            "kind":kind,"execution_mode":mode,
            "observed_taker_allowed":item.get("observed_taker_allowed") is True,
            "simulation_taker_allowed":item.get("venue_taker_allowed") is True,
            "counterfactual_only":(
                item.get("venue_taker_allowed") is True
                and item.get("observed_taker_allowed") is not True),
            "observed_venue_mode":item.get("observed_mode"),
            "observed_account_mode":item.get("observed_account_mode"),
            "simulation_venue_mode":item.get("simulation_mode"),
            "simulation_account_mode":item.get("simulation_account_mode"),
            "paper_venue_counterfactual":item.get("paper_counterfactual") is True,
            "paper_account_counterfactual":item.get("paper_account_counterfactual") is True,
            "transport_delay_ms":item["transport_ms"],
            "market_end_ms":int(m.get("end_timestamp_ms") or 0),
            "mandatory_taker_delay_ns":item["terms"].get("mandatory_taker_delay_ns") if item["terms"] else None,
            "inter_leg_skew_ms":item["skew_ms"],"leg_order":item["order"],
            "deep_requested_shares":requested_q,"target_shares":q,"state":"CENSORED",
            "lifecycle":["CREATED"],"semantic_fingerprint":item["fingerprint"],
            "fee_rounding":"MATCHED_QUANTITY_5DP_MIN_0.00001_USDC",
            "taker_rebate_used_in_entry_gate":False,
        }
        if not item["venue_taker_allowed"]:
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
        # The verified V2 production mode is USDC-value fee collection. Keep
        # the legacy shares-on-buy formula only as sensitivity, never as an
        # executable PAPER assumption.
        if buy and buy_collection_mode!="USDC_VALUE":
            base["state"]="CENSORED_BUY_FEE_COLLECTION_UNVERIFIED";return base
        yes,no=str(current.get("yes_token") or ""),str(current.get("no_token") or "")
        origin=int(c.get("receive_wall_ms") or 0)
        target=int(item["target_ms"]);skew=int(item["skew_ms"])
        if target<=0 or q<self.args.minimum_shares:
            base["state"]="NO_TRADE_SIZE";return base

        base["lifecycle"]+=["SENT","ACK_PENDING","PENDING_DELAY"]
        y0=book_point(self.book,mid,yes,target,side,deep=getattr(self,"deep",None),origin_ms=origin)
        n0=book_point(self.book,mid,no,target,side,deep=getattr(self,"deep",None),origin_ms=origin)
        if y0 is None or n0 is None:
            base["state"]="CENSORED_ARRIVAL_BOOK";return base
        if max(target-int(y0["ts"]),target-int(n0["ts"]))>self.args.maximum_book_age_ms:
            base["state"]="CENSORED_STALE_ARRIVAL";return base
        if abs(int(y0["ts"])-int(n0["ts"]))>self.args.maximum_leg_skew_ms:
            base["state"]="CENSORED_BOOK_SKEW";return base

        # Revalidate the requested q against the full causal L10 ladders.  The
        # worst consumed level becomes the marketable-limit bound for each leg.
        candidate_l10=max(0.0,float(c.get("executable_shares_l10") or 0.0))
        requires_deep=q>candidate_l10+1e-9
        deep_ready=(y0.get("depth_source")=="CAUSAL_DEEP_REPLAY_1024"
                    and n0.get("depth_source")=="CAUSAL_DEEP_REPLAY_1024")
        base["arrival_depth_source"]=(
            "CAUSAL_DEEP_REPLAY_1024" if deep_ready else "CAUSAL_L10")
        base["deep_replay_required"]=requires_deep
        base["deep_replay_available"]=deep_ready
        if requires_deep and not deep_ready:
            base["state"]="CENSORED_DEEP_REPLAY_UNAVAILABLE";return base
        yplan=fok_sweep(y0["levels"],q,side,None)
        nplan=fok_sweep(n0["levels"],q,side,None)
        if not yplan["filled"] or not nplan["filled"]:
            base.update(
                state="CENSORED_CAUSAL_DEPTH",
                causal_yes_levels_available=len(y0["levels"]),
                causal_no_levels_available=len(n0["levels"]),
            )
            return base
        yfee=sweep_fee_usdc(yplan,rate,exp);nfee=sweep_fee_usdc(nplan,rate,exp)
        if not(math.isfinite(yfee) and math.isfinite(nfee)):
            base["state"]="CENSORED_FEE_INVALID";return base
        fees_total=yfee+nfee
        gross_cash=(q-float(yplan["notional"])-float(nplan["notional"])) if buy else (
            float(yplan["notional"])+float(nplan["notional"])-q)
        edge_before_reserve=(gross_cash-fees_total)/q
        edge=edge_before_reserve-self.args.reserve_per_share
        base.update({
            "revalidation_wall_ms":target,
            "revalidation_yes_price":yplan["vwap"],
            "revalidation_no_price":nplan["vwap"],
            "revalidation_yes_worst_price":yplan["worst_price"],
            "revalidation_no_worst_price":nplan["worst_price"],
            "revalidation_yes_levels_used":yplan["levels_used"],
            "revalidation_no_levels_used":nplan["levels_used"],
            "revalidation_fee_total_usdc":fees_total,
            "revalidation_fee_per_share":fees_total/q,
            "buy_fee_collection_mode":buy_collection_mode if buy else None,
            "revalidation_edge_before_reserve":edge_before_reserve,
            "revalidation_edge_after_reserve":edge,
            "causal_multilevel_fok":True,
        })
        if edge<=0:
            base["state"]="REVALIDATION_REJECTED"
            base["lifecycle"].append("REVALIDATION_REJECTED");return base
        base["lifecycle"].append("ARRIVAL_REVALIDATED")

        limits={"YES":float(yplan["worst_price"]),"NO":float(nplan["worst_price"])}
        # Batch shares one request timestamp; parallel uses only measured
        # inter-leg skew; sequential pays one additional transport arm.
        times,order_sequence=leg_arrival_times(
            mode,item["order"],target,int(item["transport_ms"]),skew)

        fills={}
        entry_cash=0.0
        entry_fees=0.0
        weighted_volume_total=0.0
        for leg in order_sequence:
            token=yes if leg=="YES" else no
            point=book_point(
                self.book,mid,token,times[leg],side,deep=getattr(self,"deep",None),origin_ms=origin)
            sweep=fok_fill(point,side=side,limit=limits[leg],quantity=q,
                           target_ms=times[leg],maximum_book_age_ms=self.args.maximum_book_age_ms)
            ok=bool(sweep.get("filled"))
            fills[leg]={
                "filled":ok,"wall_ms":times[leg],"gross_quantity":q,
                "net_position_shares":q if ok else 0.0,
                "vwap":sweep.get("vwap"),"worst_price":sweep.get("worst_price"),
                "levels_used":sweep.get("levels_used"),"limit_price":limits[leg],
            }
            base["lifecycle"].append(f"{leg}_{'FILLED' if ok else 'REJECTED'}")
            if ok:
                leg_cash,leg_fee=entry_cashflow(side,sweep,rate,exp)
                if not(math.isfinite(leg_cash) and math.isfinite(leg_fee)):
                    base["state"]="CENSORED_FEE_INVALID";return base
                entry_cash+=leg_cash;entry_fees+=leg_fee
                if buy and isinstance(sweep.get("vwap"),(int,float)):
                    weighted_volume_total+=weighted_volume(
                        q,float(sweep["vwap"]),category_weight=2.3)

        filled=[leg for leg,v in fills.items() if v["filled"]]
        base["legs"]=fills
        base["entry_taker_fee_usdc"]=entry_fees
        base["taker_weighted_volume_counterfactual"]=weighted_volume_total
        base["taker_rebate_counterfactual_by_tier"]={
            name:entry_fees*fraction for _,fraction,name in TAKER_REBATE_TIERS
        }
        rebate_verified,rebate_fraction,rebate_tier=verified_taker_rebate(
            getattr(self.args,"fee_reward_registry",None),self.args.model_sha,time.time_ns()//1_000_000)
        base["taker_rebate_tier_verified"]=rebate_verified
        base["taker_rebate_fraction_reference"]=rebate_fraction if rebate_verified else 0.0
        base["taker_rebate_tier"]=rebate_tier if rebate_verified else None
        base["taker_rebate_reference_pusd"]=(
            entry_fees*rebate_fraction if rebate_verified else 0.0)
        # A verified tier is not the same thing as trade-attributed realized
        # rebate evidence. Keep the reference visible, but do not let it lift
        # allocator PnL until a realized payout can be independently attributed.
        base["taker_rebate_allocatable_to_pnl"]=False
        base["verified_ancillary_taker_rebate_pusd"]=0.0
        base["taker_rebate_pnl_semantics"]="TIER_VERIFIED_PAYOUT_NOT_TRADE_ATTRIBUTED"

        if len(filled)==2:
            redemption=q if buy else -q
            pnl=entry_cash+redemption
            base.update(state="COMPLETE_PAIRED",paired_execution=True,
                        entry_cashflow=entry_cash,redemption_cashflow=redemption,
                        execution_pnl_pre_reserve=pnl,
                        execution_pnl_after_reserve=pnl-q*self.args.reserve_per_share,
                        unwind_pnl=0.0,unwind_fee_usdc=0.0)
            base["lifecycle"].append("COMPLETE")
            return base
        if len(filled)==0:
            base["state"]="BOTH_REJECTED";base["paired_execution"]=False
            base["lifecycle"].append("REJECTED");return base

        first=filled[0];token=yes if first=="YES" else no
        unwind_at=max(times.values())+self.args.unwind_delay_ms
        point=book_point(
            self.book,mid,token,unwind_at,side,deep=getattr(self,"deep",None),origin_ms=origin)
        if point is None:
            base["state"]="ONE_LEG_UNWIND_CENSORED";base["paired_execution"]=False
            base["lifecycle"].append("UNWIND_CENSORED");return base
        unwind_side="SELL" if side=="BUY" else "BUY"
        unwind_sweep=fok_sweep(point["unwind_levels"],q,unwind_side,None)
        if not unwind_sweep["filled"]:
            base["state"]="ONE_LEG_UNWIND_DEPTH_FAILURE";base["paired_execution"]=False
            base["lifecycle"].append("UNWIND_DEPTH_FAILURE");return base
        u,unwind_fee=unwind_cashflow(side,unwind_sweep,rate,exp)
        if not(math.isfinite(u) and math.isfinite(unwind_fee)):
            base["state"]="ONE_LEG_UNWIND_CENSORED";base["paired_execution"]=False
            return base
        total=entry_cash+u
        base.update(
            state="ONE_LEG_UNWOUND",paired_execution=False,
            first_filled_leg=first,unwind_wall_ms=unwind_at,
            unwind_price=unwind_sweep.get("vwap"),
            unwind_worst_price=unwind_sweep.get("worst_price"),
            unwind_levels_used=unwind_sweep.get("levels_used"),
            entry_cashflow=entry_cash,unwind_pnl=u,unwind_fee_usdc=unwind_fee,
            execution_pnl_pre_reserve=total,
            execution_pnl_after_reserve=total-q*self.args.reserve_per_share)
        base["lifecycle"]+=["UNWIND_SENT","UNWIND_FILLED","COMPLETE_WITH_LEGGING"]
        return base

    def evaluate_ready(self):
        remain=[]
        for item in self.pending:
            target=item.get("target_ms")
            if target is None:
                row=self.evaluate(item)
            else:
                times,_=leg_arrival_times(
                    str(item.get("execution_mode") or "SEQUENTIAL"),
                    str(item.get("order") or "YES_FIRST"),
                    int(target),int(item.get("transport_ms") or 0),int(item.get("skew_ms") or 0))
                needed=max(times.values())+self.args.unwind_delay_ms
                if self.book.watermark_ms<needed:
                    remain.append(item);continue
                row=self.evaluate(item)
            with self.args.output.open("a",encoding="utf-8") as h:
                h.write(json.dumps(row,sort_keys=True)+"\n")
            self.rows.append(row)
        self.pending=remain

    def publish(self):
        states=Counter(str(r.get("state")) for r in self.rows)
        by_delay=defaultdict(list);by_skew=defaultdict(list);by_mode=defaultdict(list)
        for r in self.rows:
            by_delay[str(r.get("mandatory_taker_delay_ns"))].append(r)
            by_skew[str(r.get("inter_leg_skew_ms"))].append(r)
            by_mode[str(r.get("execution_mode") or "SEQUENTIAL")].append(r)
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
        atomic_json(self.args.status,{
            "schema":STATUS_SCHEMA,"model_sha":self.args.model_sha,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,
            "real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW",
            "state":"COLLECTING","timestamp_ms":time.time_ns()//1_000_000,
            "pending":len(self.pending),"evaluated":len(self.rows),"states":dict(states),
            "by_mandatory_delay_ns":{k:summary(v) for k,v in sorted(by_delay.items())},
            "by_inter_leg_skew_ms":{k:summary(v) for k,v in sorted(by_skew.items(),key=lambda x:int(x[0]))},
            "by_execution_mode":{k:summary(v) for k,v in sorted(by_mode.items())},
            "fee_semantics":"CLOB_V2_MATCH_TIME_USDC_VALUE_ROUNDED_5DP",
            "multileg_semantics":"CAUSAL_MULTILEVEL_FOK_SEQUENTIAL_PARALLEL_BATCH_WITH_INDEPENDENT_RESULTS_AND_UNWIND",
            "unknown_terms_policy":"FAIL_CLOSED",
            "counterfactual_semantics":"OBSERVED_AND_PAPER_SIMULATION_EXECUTABILITY_RECORDED_SEPARATELY",
            "counterfactual_only_scenarios":sum(r.get("counterfactual_only") is True for r in self.rows),
            "status_window_scenarios":self.args.status_window_scenarios,
            "status_window_size":len(self.rows),
            "seen_candidate_limit":self.args.seen_candidate_limit,
            "seen_candidate_count":len(self.seen),
            "restart_policy":"FOLLOW_NEW_CANDIDATES_ONLY" if self.resumed_follow_new_only else "INITIAL_TAPE_DRAIN",
        })

    def run(self):
        while True:
            self.book.poll()
            if self.deep is not None:self.deep.poll()
            self.ingest();self.evaluate_ready();self.publish()
            time.sleep(self.args.interval_ms/1000.0)


def parse_ints(value:str)->list[int]:
    return sorted({int(x) for x in value.split(",") if int(x)>=0})


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates",type=Path,required=True)
    ap.add_argument("--book-tape",type=Path,required=True)
    ap.add_argument("--deep-book-snapshots",type=Path)
    ap.add_argument("--selection",type=Path,required=True)
    ap.add_argument("--market-terms-root",type=Path,required=True)
    ap.add_argument("--venue-mode",type=Path,required=True)
    ap.add_argument("--exchange-semantics",type=Path,required=True)
    ap.add_argument("--fee-reward-registry",type=Path)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--transport-delay-ms",default="1,2,5,10")
    ap.add_argument("--inter-leg-skew-ms",default="0,1,2,5,10")
    ap.add_argument("--transport-modes",default="SEQUENTIAL,PARALLEL,BATCH")
    ap.add_argument("--unwind-delay-ms",type=int,default=2)
    ap.add_argument("--maximum-book-age-ms",type=int,default=100)
    ap.add_argument("--maximum-leg-skew-ms",type=int,default=100)
    ap.add_argument("--reserve-per-share",type=float,default=.0005)
    ap.add_argument("--minimum-shares",type=float,default=1.0)
    ap.add_argument("--maximum-shares",type=float,default=1000.0)
    ap.add_argument("--interval-ms",type=int,default=5)
    ap.add_argument("--status-window-scenarios",type=int,default=50_000)
    ap.add_argument("--status-restore-bytes",type=int,default=67_108_864)
    ap.add_argument("--seen-candidate-limit",type=int,default=100_000)
    args=ap.parse_args()
    args.transport_delay_ms=parse_ints(args.transport_delay_ms)
    args.inter_leg_skew_ms=parse_ints(args.inter_leg_skew_ms)
    args.transport_modes=sorted({x.strip().upper() for x in args.transport_modes.split(",") if x.strip()})
    if len(args.model_sha)!=40 or any(c not in "0123456789abcdef" for c in args.model_sha):
        raise SystemExit("invalid sha")
    if not(args.transport_delay_ms and args.inter_leg_skew_ms
           and args.transport_modes and set(args.transport_modes)<= {"SEQUENTIAL","PARALLEL","BATCH"}
           and 0<=args.unwind_delay_ms<=5000 and 1<=args.maximum_book_age_ms<=5000
           and 0<=args.maximum_leg_skew_ms<=5000 and 0<=args.reserve_per_share<1
           and 0<args.minimum_shares<=args.maximum_shares and 1<=args.interval_ms<=1000
           and 100<=args.status_window_scenarios<=1_000_000
           and 1_048_576<=args.status_restore_bytes<=1_073_741_824
           and 1_000<=args.seen_candidate_limit<=1_000_000):
        raise SystemExit("invalid arguments")
    Shadow(args).run();return 0


if __name__=="__main__":
    raise SystemExit(main())
