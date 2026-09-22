#!/usr/bin/env python3
"""Event-triggered full-depth PAPER sizing audit for pure complete-set arbitrage.

The hot path intentionally uses bounded causal L10 depth. This shadow only wakes
for detected arb episodes, fetches the full public CLOB books, and solves the
same marginal-edge sizing problem over all available levels. It measures whether
expanding hot depth is economically worth the latency/memory cost.

Capacity research only; fetched books are not causal execution evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_pm_repricing_common import atomic_json
from v7_pure_arb_economics import raw_fee_per_share, rounded_fee_usdc, tail_jsonl
from v7_clob_public_batch import fetch_books, full_book as parse_full_book

CYCLE_SCHEMAS={
    "polymarket_v7_pure_arb_paper_cycle_v2",
    "polymarket_v7_pure_arb_paper_cycle_v3",
}
STATUS_SCHEMA="polymarket_v7_pure_arb_deep_sizing_status_v1"
ROW_SCHEMA="polymarket_v7_pure_arb_deep_sizing_cycle_v1"


def load(path:Path)->dict[str,Any]:
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}


def fee(price:float,rate:float,exponent:float)->float:
    return raw_fee_per_share(price,rate,exponent)


def selection_map(v:dict[str,Any],sha:str)->dict[str,dict[str,Any]]:
    if (v.get("schema")!="polymarket_v7_multi_crypto_book_selection_v1"
        or v.get("model_sha")!=sha or v.get("paper_only") is not True
        or v.get("authenticated_execution") is not False
        or v.get("real_order_submission") is not False
        or v.get("execution_authority") is not False):
        return {}
    return {str(r.get("market_id")):r for r in v.get("markets") or []
            if isinstance(r,dict) and r.get("market_id")}


def fee_params(row:dict[str,Any])->tuple[float,float]|None:
    fs=row.get("fee_schedule")
    if isinstance(fs,dict):
        try:r=float(fs["rate"]);e=float(fs.get("exponent",1.0))
        except (KeyError,TypeError,ValueError):return None
        if math.isfinite(r) and 0<=r<=1 and math.isfinite(e) and e>=0:return r,e
    if row.get("fees_enabled_explicit") is True and row.get("fees_enabled") is False:
        return 0.0,1.0
    return None


def sweep(a:list[tuple[float,float]],b:list[tuple[float,float]],rate:float,exponent:float,
          reserve:float,buy:bool,max_shares:float)->dict[str,float]:
    i=j=0;ar=br=0.0;q=0.0;pnl=0.0;not_a=not_b=0.0;marginal=0.0
    while i<len(a) and j<len(b) and q<max_shares-1e-12:
        if ar<=1e-12:ar=a[i][1]
        if br<=1e-12:br=b[j][1]
        pa,pb=a[i][0],b[j][0]
        take=min(ar,br,max_shares-q)
        if take<=1e-12:break
        fee_a=rounded_fee_usdc(take,pa,rate,exponent)
        fee_b=rounded_fee_usdc(take,pb,rate,exponent)
        if not(math.isfinite(fee_a) and math.isfinite(fee_b)):break
        fees=(fee_a+fee_b)/take
        gross=(1-pa-pb-fees) if buy else (pa+pb-1-fees)
        edge=gross-reserve
        if edge<=1e-12:break
        q+=take;pnl+=take*edge;not_a+=take*pa;not_b+=take*pb;marginal=edge
        ar-=take;br-=take
        if ar<=1e-12:i+=1
        if br<=1e-12:j+=1
    return {
        "shares":q,"locked_pnl_after_reserve":pnl,
        "average_edge_per_share":pnl/q if q else 0.0,
        "marginal_edge_per_share":marginal,
        "vwap_1":not_a/q if q else 0.0,"vwap_2":not_b/q if q else 0.0,
    }


class Tail:
    def __init__(self,path:Path,sha:str,*,start_at_end:bool=False):
        self.path,self.sha,self.handle=path,sha,None
        self.start_at_end_pending=bool(start_at_end)
    def poll(self):
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
                if not raw or not raw.endswith(b"\n"):self.handle.seek(pos);break
                try:r=json.loads(raw)
                except (ValueError,UnicodeDecodeError):continue
                if (isinstance(r,dict) and r.get("schema") in CYCLE_SCHEMAS
                    and r.get("model_sha")==self.sha and r.get("paper_only") is True
                    and r.get("authenticated_execution") is False
                    and r.get("real_order_submission") is False):out.append(r)
            try:
                old,cur=os.fstat(self.handle.fileno()),self.path.stat()
                if (old.st_dev,old.st_ino)==(cur.st_dev,cur.st_ino):
                    if cur.st_size<self.handle.tell():self.handle.seek(0)
                    return out
            except OSError:return out
            self.handle.close();self.handle=None
        return out


class Shadow:
    def __init__(self,args):
        self.args=args
        try:resumed=args.output.exists() and args.output.stat().st_size>0
        except OSError:resumed=False
        self.tail=Tail(args.candidates,args.model_sha,start_at_end=resumed)
        self.rows=deque(maxlen=args.status_window_cycles)
        self.seen=set()
        self.seen_order=deque()
        self.resumed_follow_new_only=resumed
        args.output.parent.mkdir(parents=True,exist_ok=True)
        self._restore()
    def _restore(self):
        for r in tail_jsonl(
            self.args.output,max_rows=self.args.status_window_cycles,
            max_bytes=self.args.status_restore_bytes):
            if r.get("schema")==ROW_SCHEMA and r.get("model_sha")==self.args.model_sha:
                self.rows.append(r)
    def _mark_seen(self,cid:str)->bool:
        if not cid or cid in self.seen:return False
        while len(self.seen_order)>=self.args.seen_candidate_limit:
            old=self.seen_order.popleft();self.seen.discard(old)
        self.seen.add(cid);self.seen_order.append(cid)
        return True
    def evaluate(self,c):
        mid=str(c.get("market_id") or "");kind=str(c.get("kind") or "")
        detected=int(c.get("receive_wall_ms") or 0)
        cid=f"{mid}:{kind}:{detected}"
        if not self._mark_seen(cid):return None
        m=selection_map(load(self.args.selection),self.args.model_sha).get(mid)
        base={"schema":ROW_SCHEMA,"model_sha":self.args.model_sha,"paper_only":True,
              "authenticated_execution":False,"real_order_submission":False,
              "real_capital_at_risk":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
              "candidate_id":cid,"market_id":mid,"kind":kind,"detected_wall_ms":detected,
              "state":"CENSORED_METADATA"}
        if m is None:return base
        fp=fee_params(m)
        if fp is None:base["state"]="CENSORED_FEE";return base
        yes,no=str(m.get("yes_token") or ""),str(m.get("no_token") or "")
        started=time.time_ns()//1_000_000
        batch=fetch_books(
            self.args.clob_url,[yes,no],self.args.timeout_seconds,
            chunk_size=2,user_agent="polymarket-v7-deep-sizing-shadow")
        y=parse_full_book(batch.get(yes));n=parse_full_book(batch.get(no))
        fetched=time.time_ns()//1_000_000
        base["fetch_complete_wall_ms"]=fetched;base["fetch_delay_from_detection_ms"]=max(0,fetched-detected)
        if y is None or n is None:base["state"]="CENSORED_BOOK";return base
        rate,exponent=fp
        buy=kind=="BUY_COMPLETE_SET"
        ladd_y=y["asks"] if buy else y["bids"];ladd_n=n["asks"] if buy else n["bids"]
        cap=self.args.maximum_shares if buy else min(self.args.maximum_shares,self.args.prefunded_complete_set_shares)
        result=sweep(ladd_y,ladd_n,rate,exponent,self.args.reserve_per_share,buy,cap)
        l10=float(c.get("executable_shares_l10") or c.get("executable_shares_l1") or 0)
        l10_pnl=float(c.get("conservative_locked_pnl_after_reserve") or 0)
        base.update(result)
        base.update({"state":"EVALUATED","l10_detected_shares":l10,"l10_detected_pnl":l10_pnl,
                     "incremental_shares_vs_l10":max(0.0,result["shares"]-l10),
                     "incremental_pnl_vs_l10":result["locked_pnl_after_reserve"]-l10_pnl,
                     "capacity_semantics":"NONCAUSAL_FULL_BOOK_AUDIT_NOT_EXECUTION_EVIDENCE",
          "status_window_cycles":self.args.status_window_cycles,
          "status_window_size":len(self.rows),
          "seen_candidate_limit":self.args.seen_candidate_limit,
          "seen_candidate_count":len(self.seen),
          "restart_policy":"FOLLOW_NEW_CANDIDATES_ONLY" if self.resumed_follow_new_only else "INITIAL_TAPE_DRAIN"})
        return base
    def publish(self):
        evaluated=[r for r in self.rows if r.get("state")=="EVALUATED"]
        atomic_json(self.args.status,{"schema":STATUS_SCHEMA,"model_sha":self.args.model_sha,
          "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
          "real_capital_at_risk":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
          "state":"COLLECTING","timestamp_ms":time.time_ns()//1_000_000,
          "cycles":len(self.rows),"evaluated":len(evaluated),
          "states":dict(Counter(str(r.get("state")) for r in self.rows)),
          "mean_incremental_shares_vs_l10":sum(float(r.get("incremental_shares_vs_l10") or 0) for r in evaluated)/len(evaluated) if evaluated else None,
          "sum_incremental_pnl_vs_l10":sum(float(r.get("incremental_pnl_vs_l10") or 0) for r in evaluated),
          "mean_fetch_delay_ms":sum(float(r.get("fetch_delay_from_detection_ms") or 0) for r in evaluated)/len(evaluated) if evaluated else None,
          "capacity_semantics":"NONCAUSAL_FULL_BOOK_AUDIT_NOT_EXECUTION_EVIDENCE"})
    def run(self):
        while True:
            for c in self.tail.poll():
                row=self.evaluate(c)
                if row is not None:
                    with self.args.output.open("a",encoding="utf-8") as h:h.write(json.dumps(row,sort_keys=True)+"\n")
                    self.rows.append(row)
            self.publish();time.sleep(max(.05,self.args.interval_seconds))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates",type=Path,required=True)
    ap.add_argument("--selection",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--status",type=Path,required=True)
    ap.add_argument("--clob-url",default="https://clob.polymarket.com")
    ap.add_argument("--reserve-per-share",type=float,default=.0005)
    ap.add_argument("--maximum-shares",type=float,default=10000)
    ap.add_argument("--prefunded-complete-set-shares",type=float,default=1000)
    ap.add_argument("--timeout-seconds",type=float,default=2)
    ap.add_argument("--interval-seconds",type=float,default=.25)
    ap.add_argument("--status-window-cycles",type=int,default=20000)
    ap.add_argument("--status-restore-bytes",type=int,default=67108864)
    ap.add_argument("--seen-candidate-limit",type=int,default=100000)
    a=ap.parse_args()
    if len(a.model_sha)!=40 or not(0<=a.reserve_per_share<1 and a.maximum_shares>0
        and a.prefunded_complete_set_shares>0 and .1<=a.timeout_seconds<=10 and .05<=a.interval_seconds<=60
        and 100<=a.status_window_cycles<=1_000_000
        and 1_048_576<=a.status_restore_bytes<=1_073_741_824
        and 1_000<=a.seen_candidate_limit<=1_000_000):
        raise SystemExit("invalid arguments")
    Shadow(a).run();return 0
if __name__=="__main__":raise SystemExit(main())
