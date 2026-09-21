#!/usr/bin/env python3
"""Pure Polymarket arbitrage scanner/backtest.

No prediction. No ML. Every reported trade has a deterministic payoff floor.

Pre-settlement:
  BUY_COMPLETE_SET:  buy YES + NO when asks + fees + reserve < 1.
  SELL_COMPLETE_SET: sell prefunded/minted YES + NO when bids - fees - reserve > 1.

After exact settlement-source fixing:
  BUY_WINNER: buy winning token below redemption value 1.
  SELL_LOSER: split/prefund a complete set, sell loser, redeem winner.

The scanner is zero-authority PAPER/research only.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_pure_arb_scan_v1"


def jlines(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                try:
                    row=json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row,dict):
                    yield row
    except OSError:
        return


def load(path: Path) -> dict[str, Any]:
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):
        return {}
    return value if isinstance(value,dict) else {}


def fee(price: float, rate: float, exponent: float) -> float:
    if not (math.isfinite(price) and 0 < price < 1 and 0 <= rate <= 1 and exponent >= 0):
        return math.nan
    return rate*(price*(1-price))**exponent if rate else 0.0


def fee_map(registry: dict[str,Any]) -> dict[str,tuple[float,float]]:
    out={}
    for row in registry.get("markets") or []:
        if not isinstance(row,dict): continue
        f=row.get("fee") if isinstance(row.get("fee"),dict) else {}
        if f.get("verified") is not True: continue
        try:r=float(f["rate"]);e=float(f["exponent"])
        except (KeyError,TypeError,ValueError): continue
        if math.isfinite(r) and 0<=r<=1 and math.isfinite(e) and e>=0:
            out[str(row.get("market_id") or "")]=(r,e)
    return out


def selection_map(selection: dict[str,Any]) -> dict[str,dict[str,Any]]:
    out={}
    for row in selection.get("markets") or []:
        if not isinstance(row,dict): continue
        mid=str(row.get("market_id") or "")
        yes=str(row.get("yes_token") or "")
        no=str(row.get("no_token") or "")
        if mid and yes and no and yes!=no:
            out[mid]={**row,"yes_token":yes,"no_token":no}
    return out


def outcome_map(oracle: dict[str,Any]) -> dict[str,dict[str,Any]]:
    raw=oracle.get("settlement_outcomes")
    return raw if isinstance(raw,dict) else {}


def valid_book(row: dict[str,Any], sha: str) -> bool:
    return (
        row.get("schema")=="polymarket_v7_causal_book_observation_v1"
        and row.get("model_sha")==sha
        and row.get("paper_only") is True
        and row.get("authenticated_execution") is False
        and row.get("real_order_submission") is False
        and row.get("execution_authority")=="ZERO_AUTHORITY_RESEARCH_ONLY"
        and row.get("valid") is True
        and row.get("lineage_continuous") is True
    )


def snapshot(row:dict[str,Any]) -> dict[str,Any] | None:
    try:
        bid=float(row["best_bid"]); ask=float(row["best_ask"])
        bq=float(row.get("bid_depth_l1") or 0); aq=float(row.get("ask_depth_l1") or 0)
        ts=int(row["receive_wall_ms"]); epoch=int(row["connection_epoch"])
    except (KeyError,TypeError,ValueError,OverflowError):
        return None
    if not (0<bid<ask<1 and bq>0 and aq>0 and ts>0 and epoch>0): return None
    return {"bid":bid,"ask":ask,"bid_q":bq,"ask_q":aq,"ts":ts,"epoch":epoch}


def candidate(mid:str, m:dict[str,Any], y:dict[str,Any], n:dict[str,Any],
              rate:float,exp:float,size:float,reserve:float,outcome:dict[str,Any]|None):
    if y["epoch"]!=n["epoch"]: return []
    t=max(y["ts"],n["ts"])
    if abs(y["ts"]-n["ts"])>100: return []
    fyb, fnb = fee(y["ask"],rate,exp), fee(n["ask"],rate,exp)
    fys, fns = fee(y["bid"],rate,exp), fee(n["bid"],rate,exp)
    if not all(math.isfinite(x) for x in (fyb,fnb,fys,fns)): return []
    rows=[]

    q=min(size,y["ask_q"],n["ask_q"])
    edge=1-y["ask"]-n["ask"]-fyb-fnb-reserve
    if q>0 and edge>0:
        rows.append({"kind":"BUY_COMPLETE_SET","market_id":mid,"time_ms":t,
                     "shares":q,"edge_per_share":edge,"locked_pnl":q*edge,
                     "yes_price":y["ask"],"no_price":n["ask"]})

    q=min(size,y["bid_q"],n["bid_q"])
    edge=y["bid"]+n["bid"]-1-fys-fns-reserve
    if q>0 and edge>0:
        rows.append({"kind":"SELL_COMPLETE_SET","market_id":mid,"time_ms":t,
                     "shares":q,"edge_per_share":edge,"locked_pnl":q*edge,
                     "yes_price":y["bid"],"no_price":n["bid"],
                     "requires_prefunded_or_atomic_split":True})

    if isinstance(outcome,dict) and outcome.get("valid") is True:
        winner=str(outcome.get("winning_outcome") or "")
        win=y if winner=="YES" else n if winner=="NO" else None
        lose=n if winner=="YES" else y if winner=="NO" else None
        if win is not None and lose is not None:
            fw=fee(win["ask"],rate,exp)
            q=min(size,win["ask_q"])
            edge=1-win["ask"]-fw-reserve
            if q>0 and edge>0:
                rows.append({"kind":"BUY_WINNER","market_id":mid,"time_ms":t,
                             "winning_outcome":winner,"shares":q,
                             "edge_per_share":edge,"locked_pnl":q*edge,
                             "winner_ask":win["ask"]})
            fl=fee(lose["bid"],rate,exp)
            q=min(size,lose["bid_q"])
            edge=lose["bid"]-fl-reserve
            if q>0 and edge>0:
                rows.append({"kind":"SELL_LOSER","market_id":mid,"time_ms":t,
                             "winning_outcome":winner,"shares":q,
                             "edge_per_share":edge,"locked_pnl":q*edge,
                             "loser_bid":lose["bid"],
                             "requires_prefunded_or_atomic_split":True})
    for r in rows:
        r.update(asset=str(m.get("asset") or ""),horizon=str(m.get("horizon") or ""),
                 fee_rate=rate,fee_exponent=exp,reserve_per_share=reserve)
    return rows


def run(args):
    selection=selection_map(load(args.selection))
    fees=fee_map(load(args.fee_registry))
    outcomes=outcome_map(load(args.oracle_status)) if args.oracle_status else {}
    token_index={}
    for mid,m in selection.items():
        token_index[m["yes_token"]]=(mid,"YES")
        token_index[m["no_token"]]=(mid,"NO")
    latest={}
    emitted=set()
    trades=[]
    for row in jlines(args.book_tape):
        if not valid_book(row,args.model_sha): continue
        key=token_index.get(str(row.get("token_id") or ""))
        if key is None: continue
        snap=snapshot(row)
        if snap is None: continue
        mid,side=key
        latest[(mid,side)]=snap
        y=latest.get((mid,"YES")); n=latest.get((mid,"NO"))
        if y is None or n is None or mid not in fees: continue
        rate,exp=fees[mid]
        for trade in candidate(mid,selection[mid],y,n,rate,exp,args.target_shares,
                               args.reserve_per_share,outcomes.get(mid)):
            # one record per market/kind/exact paired book state
            identity=(mid,trade["kind"],y["ts"],n["ts"])
            if identity in emitted: continue
            emitted.add(identity); trades.append(trade)

    by_kind={}
    for r in trades:
        s=by_kind.setdefault(r["kind"],{"opportunities":0,"locked_pnl":0.0,"shares":0.0,
                                        "max_edge_per_share":None})
        s["opportunities"]+=1;s["locked_pnl"]+=r["locked_pnl"];s["shares"]+=r["shares"]
        edge=r["edge_per_share"]
        s["max_edge_per_share"]=edge if s["max_edge_per_share"] is None else max(s["max_edge_per_share"],edge)
    result={"schema":SCHEMA,"paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"real_capital_at_risk":False,
            "automatic_promotion":False,"model_sha":args.model_sha,
            "book_tape":str(args.book_tape),"markets":len(selection),
            "markets_with_verified_fee":len(fees),"opportunities":len(trades),
            "locked_pnl_sum_naive_eventwise":sum(r["locked_pnl"] for r in trades),
            "note":"Eventwise sum is diagnostic, not portfolio PnL; overlapping book states are not independently executable.",
            "by_kind":by_kind,"top":sorted(trades,key=lambda r:r["locked_pnl"],reverse=True)[:100]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:result[k] for k in ("opportunities","by_kind")},sort_keys=True))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--book-tape",type=Path,required=True)
    ap.add_argument("--selection",type=Path,required=True)
    ap.add_argument("--fee-registry",type=Path,required=True)
    ap.add_argument("--oracle-status",type=Path)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--target-shares",type=float,default=5.0)
    ap.add_argument("--reserve-per-share",type=float,default=0.0005)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    if len(a.model_sha)!=40 or any(ch not in "0123456789abcdef" for ch in a.model_sha):
        raise SystemExit("exact SHA required")
    if not (a.target_shares>0 and 0<=a.reserve_per_share<1):
        raise SystemExit("invalid economics")
    run(a)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
