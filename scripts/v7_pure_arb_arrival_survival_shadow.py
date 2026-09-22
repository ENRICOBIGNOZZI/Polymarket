#!/usr/bin/env python3
"""Causal PAPER arrival-survival evaluator for pure complete-set arbitrage.

Tails detected pure-arb episodes and the canonical causal book tape. For each
episode it evaluates fixed arrival-delay arms without changing the live reserve
or execution rule. This turns the opportunity funnel into:

detected -> still valid at arrival -> paired depth -> captured edge/PnL.

Zero authority. No orders. No automatic promotion.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_causal_book import BookTimeline
from v7_pm_repricing_common import atomic_json

CYCLE_SCHEMAS = {
    "polymarket_v7_pure_arb_paper_cycle_v2",
    "polymarket_v7_pure_arb_paper_cycle_v3",
}
STATUS_SCHEMA = "polymarket_v7_pure_arb_arrival_survival_status_v1"
ROW_SCHEMA = "polymarket_v7_pure_arb_arrival_survival_cycle_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):
        return {}
    return value if isinstance(value,dict) else {}


def market_terms(root: Path | None, market_id: str) -> dict[str, Any]:
    if root is None or not market_id:
        return {}
    value=load(root/(market_id+".json"))
    if (
        value.get("schema")!="polymarket_v7_market_execution_terms_v1"
        or value.get("market_id")!=market_id
        or value.get("state")!="VERIFIED_SNAPSHOT"
        or value.get("paper_only") is not True
    ):
        return {}
    try:
        delay_ns=int(value.get("mandatory_taker_delay_ns"))
    except (TypeError,ValueError):
        return {}
    if delay_ns<0 or delay_ns>5_000_000_000:
        return {}
    return {**value,"mandatory_taker_delay_ns":delay_ns}

def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not (math.isfinite(price) and 0<price<1 and math.isfinite(rate)
            and 0<=rate<=1 and math.isfinite(exponent) and exponent>=0):
        return math.nan
    return rate*(price*(1-price))**exponent if rate else 0.0


def fee_params(row: dict[str,Any]) -> tuple[float,float] | None:
    fee=row.get("fee_schedule")
    if isinstance(fee,dict):
        try:
            rate=float(fee["rate"]); exponent=float(fee.get("exponent",1.0))
        except (KeyError,TypeError,ValueError):
            return None
        if math.isfinite(rate) and 0<=rate<=1 and math.isfinite(exponent) and exponent>=0:
            return rate,exponent
    if row.get("fees_enabled_explicit") is True and row.get("fees_enabled") is False:
        return 0.0,1.0
    return None


def selection_map(value: dict[str,Any], sha: str) -> dict[str,dict[str,Any]]:
    if (
        value.get("schema")!="polymarket_v7_multi_crypto_book_selection_v1"
        or value.get("model_sha")!=sha
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") is not False
    ):
        return {}
    out={}
    for row in value.get("markets") or []:
        if not isinstance(row,dict): continue
        mid=str(row.get("market_id") or "")
        yes=str(row.get("yes_token") or "")
        no=str(row.get("no_token") or "")
        if mid and yes and no and yes!=no:
            out[mid]=row
    return out


class Tail:
    def __init__(self,path:Path,sha:str)->None:
        self.path,self.sha=path,sha
        self.handle=None

    def poll(self)->list[dict[str,Any]]:
        out=[]
        for _ in range(2):
            if self.handle is None:
                try:self.handle=self.path.open("rb")
                except OSError:return out
            while True:
                offset=self.handle.tell()
                raw=self.handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    self.handle.seek(offset); break
                try:row=json.loads(raw)
                except (ValueError,UnicodeDecodeError):continue
                if (
                    isinstance(row,dict)
                    and row.get("schema") in CYCLE_SCHEMAS
                    and row.get("model_sha")==self.sha
                    and row.get("paper_only") is True
                    and row.get("authenticated_execution") is False
                    and row.get("real_order_submission") is False
                ):
                    out.append(row)
            try:
                old,current=os.fstat(self.handle.fileno()),self.path.stat()
                if (old.st_dev,old.st_ino)==(current.st_dev,current.st_ino):
                    if current.st_size<self.handle.tell():self.handle.seek(0)
                    return out
            except OSError:return out
            self.handle.close();self.handle=None
        return out


class Shadow:
    def __init__(self,args:argparse.Namespace)->None:
        self.args=args
        maximum_delay=max(
            max(args.delay_arms_ms),
            5000 + max(args.transport_delay_arms_ms, default=0),
        )
        self.book=BookTimeline(args.book_tape,args.model_sha,retention_ms=maximum_delay+5000)
        self.tail=Tail(args.candidates,args.model_sha)
        self.pending:list[dict[str,Any]]=[]
        self.rows:list[dict[str,Any]]=[]
        self.seen:set[str]=set()
        self.market_cache:dict[str,dict[str,Any]]={}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.status.parent.mkdir(parents=True,exist_ok=True)
        self._restore()

    def _restore(self)->None:
        try:
            for raw in self.args.output.read_text(encoding="utf-8").splitlines():
                row=json.loads(raw)
                if row.get("schema")==ROW_SCHEMA and row.get("model_sha")==self.args.model_sha:
                    self.rows.append(row)
                    self.seen.add(str(row.get("evaluation_id") or ""))
        except (OSError,json.JSONDecodeError):
            pass

    def refresh_markets(self)->None:
        current=selection_map(load(self.args.selection),self.args.model_sha)
        self.market_cache.update(current)

    def ingest(self)->None:
        self.refresh_markets()
        for row in self.tail.poll():
            try:
                detected=int(row.get("receive_wall_ms") or 0)
            except (TypeError,ValueError):
                continue
            mid=str(row.get("market_id") or "")
            kind=str(row.get("kind") or "")
            if detected<=0 or not mid or kind not in {"BUY_COMPLETE_SET","SELL_COMPLETE_SET"}:
                continue
            base=f"{mid}:{kind}:{detected}"
            sources={delay:{"FIXED_COUNTERFACTUAL"} for delay in self.args.delay_arms_ms}
            terms=market_terms(self.args.market_terms_root,mid)
            if terms:
                venue_ms=int(terms["mandatory_taker_delay_ns"])//1_000_000
                for transport in self.args.transport_delay_arms_ms:
                    sources.setdefault(venue_ms+transport,set()).add(
                        f"VENUE_DELAY_{venue_ms}MS_PLUS_TRANSPORT_{transport}MS")
            for delay,labels in sorted(sources.items()):
                eid=f"{base}:{delay}"
                if eid in self.seen:
                    continue
                self.pending.append({
                    "evaluation_id":eid,
                    "candidate":row,
                    "delay_ms":delay,
                    "target_ms":detected+delay,
                    "delay_sources":sorted(labels),
                    "market_terms_verified":bool(terms),
                    "mandatory_taker_delay_ns":terms.get("mandatory_taker_delay_ns") if terms else None,
                })
                self.seen.add(eid)

    def evaluate_one(self,item:dict[str,Any])->dict[str,Any]:
        c=item["candidate"]; mid=str(c["market_id"]); kind=str(c["kind"])
        delay=int(item["delay_ms"]); target_ms=int(item["target_ms"])
        market=self.market_cache.get(mid)
        base={
            "schema":ROW_SCHEMA,"model_sha":self.args.model_sha,
            "paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
            "evaluation_id":item["evaluation_id"],"market_id":mid,
            "asset":str(c.get("asset") or ""),"horizon":str(c.get("horizon") or ""),
            "kind":kind,"detected_wall_ms":int(c.get("receive_wall_ms") or 0),
            "arrival_delay_ms":delay,"arrival_wall_ms":target_ms,
            "detected_edge_per_share":c.get("edge_per_share"),
            "detected_conservative_edge_per_share":c.get("conservative_edge_per_share"),
            "detected_executable_shares":float(c.get("executable_shares_l10") or c.get("executable_shares_l1") or 0.0),
            "reserve_per_share":self.args.reserve_per_share,
            "state":"CENSORED_MARKET_METADATA",
            "arrival_edge_per_share":None,"arrival_executable_shares_l1":0.0,
            "captured_pnl_l1":0.0,
        }
        if market is None:return base
        params=fee_params(market)
        if params is None:
            base["state"]="CENSORED_FEE_UNVERIFIED";return base
        yes,no=str(market.get("yes_token") or ""),str(market.get("no_token") or "")
        y=self.book.asof(mid,yes,target_ms); n=self.book.asof(mid,no,target_ms)
        if y is None or n is None:
            base["state"]="CENSORED_BOOK_UNAVAILABLE";return base
        try:
            yts,nts=int(y["receive_wall_ms"]),int(n["receive_wall_ms"])
            if abs(yts-nts)>self.args.maximum_leg_skew_ms:
                base["state"]="CENSORED_LEG_SKEW";return base
            if max(target_ms-yts,target_ms-nts)>self.args.maximum_book_age_ms:
                base["state"]="CENSORED_STALE_BOOK";return base
            if kind=="BUY_COMPLETE_SET":
                yp,np=float(y["best_ask"]),float(n["best_ask"])
                yd,nd=float(y.get("ask_depth_l1") or 0),float(n.get("ask_depth_l1") or 0)
                gross=1.0-yp-np
            else:
                yp,np=float(y["best_bid"]),float(n["best_bid"])
                yd,nd=float(y.get("bid_depth_l1") or 0),float(n.get("bid_depth_l1") or 0)
                gross=yp+np-1.0
        except (KeyError,TypeError,ValueError,OverflowError):
            base["state"]="CENSORED_INVALID_BOOK";return base
        rate,exponent=params
        fees=fee_per_share(yp,rate,exponent)+fee_per_share(np,rate,exponent)
        if not math.isfinite(fees):
            base["state"]="CENSORED_INVALID_FEE";return base
        after_fee=gross-fees
        edge=after_fee-self.args.reserve_per_share
        depth=max(0.0,min(yd,nd))
        detected=max(0.0,float(base["detected_executable_shares"] or 0.0))
        fill=min(depth,detected) if detected>0 else depth
        base.update({
            "yes_price":yp,"no_price":np,"raw_edge_per_share":gross,
            "fee_per_share":fees,"after_fee_edge_per_share":after_fee,
            "arrival_edge_per_share":edge,"arrival_executable_shares_l1":depth,
            "captured_shares_l1":fill,
        })
        if gross<=0:
            base["state"]="EDGE_GONE_RAW"
        elif after_fee<=0:
            base["state"]="EDGE_GONE_FEES"
        elif edge<=0:
            base["state"]="EDGE_GONE_RESERVE"
        elif fill<self.args.minimum_fill_shares:
            base["state"]="SURVIVED_NO_DEPTH"
        else:
            base["state"]="SURVIVED_EXECUTABLE"
            base["captured_pnl_l1"]=fill*edge
        return base

    def evaluate_ready(self)->None:
        remain=[]
        for item in self.pending:
            if self.book.watermark_ms<int(item["target_ms"]):
                remain.append(item);continue
            row=self.evaluate_one(item)
            with self.args.output.open("a",encoding="utf-8") as h:
                h.write(json.dumps(row,sort_keys=True)+"\n")
            self.rows.append(row)
        self.pending=remain

    def publish(self)->None:
        by_delay=defaultdict(list)
        for row in self.rows:by_delay[int(row.get("arrival_delay_ms") or 0)].append(row)
        delays={}
        for delay,rows in sorted(by_delay.items()):
            executable=[r for r in rows if r.get("state")=="SURVIVED_EXECUTABLE"]
            edges=[float(r["arrival_edge_per_share"]) for r in executable
                   if isinstance(r.get("arrival_edge_per_share"),(int,float))]
            delays[str(delay)]={
                "evaluated":len(rows),
                "survived_executable":len(executable),
                "survival_probability":len(executable)/len(rows) if rows else None,
                "captured_pnl_l1":sum(float(r.get("captured_pnl_l1") or 0) for r in executable),
                "mean_arrival_edge_per_share":sum(edges)/len(edges) if edges else None,
                "states":dict(__import__("collections").Counter(str(r.get("state")) for r in rows)),
            }
        reserve_curve={}
        for reserve in self.args.reserve_arms:
            key=f"{reserve:.6f}"
            counts={}
            for delay,rows in sorted(by_delay.items()):
                good=0
                for r in rows:
                    after=r.get("after_fee_edge_per_share")
                    depth=float(r.get("arrival_executable_shares_l1") or 0)
                    if isinstance(after,(int,float)) and float(after)>reserve and depth>=self.args.minimum_fill_shares:
                        good+=1
                counts[str(delay)]={"evaluated":len(rows),"survived":good,
                                    "survival_probability":good/len(rows) if rows else None}
            reserve_curve[key]=counts
        atomic_json(self.args.status,{
            "schema":STATUS_SCHEMA,"model_sha":self.args.model_sha,
            "paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"real_capital_at_risk":False,
            "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
            "state":"COLLECTING","timestamp_ms":time.time_ns()//1_000_000,
            "pending":len(self.pending),"evaluated":len(self.rows),
            "delay_arms_ms":self.args.delay_arms_ms,
            "reserve_per_share":self.args.reserve_per_share,
            "reserve_curve":reserve_curve,"by_delay":delays,
        })

    def run(self)->None:
        while True:
            self.book.poll();self.ingest();self.evaluate_ready();self.publish()
            time.sleep(max(.001,self.args.interval_ms/1000.0))


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates",type=Path,required=True)
    ap.add_argument("--book-tape",type=Path,required=True)
    ap.add_argument("--selection",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--delay-arms-ms",default="1,2,5,10,25,50")
    ap.add_argument("--reserve-per-share",type=float,default=.0005)
    ap.add_argument("--reserve-arms",default="0,0.0001,0.00025,0.0005,0.001,0.0025,0.005")
    ap.add_argument("--minimum-fill-shares",type=float,default=1.0)
    ap.add_argument("--maximum-leg-skew-ms",type=int,default=100)
    ap.add_argument("--maximum-book-age-ms",type=int,default=100)
    ap.add_argument("--interval-ms",type=int,default=5)
    args=ap.parse_args()
    args.delay_arms_ms=sorted({int(x) for x in args.delay_arms_ms.split(",") if int(x)>=0})
    args.reserve_arms=sorted({float(x) for x in args.reserve_arms.split(",") if float(x)>=0})
    if len(args.model_sha)!=40 or not args.delay_arms_ms or not args.reserve_arms:
        raise SystemExit("invalid arguments")
    if not (0<=args.reserve_per_share<1 and args.minimum_fill_shares>0
            and 0<=args.maximum_leg_skew_ms<=5000 and 1<=args.maximum_book_age_ms<=5000
            and 1<=args.interval_ms<=1000):
        raise SystemExit("invalid bounds")
    Shadow(args).run();return 0


if __name__=="__main__":
    raise SystemExit(main())
