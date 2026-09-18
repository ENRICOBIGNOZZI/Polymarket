#!/usr/bin/env python3
"""Held-out sequential replay for the price-aware settlement EV gate."""
from __future__ import annotations
from collections import defaultdict
import math
from statistics import mean
from typing import Any

from models import predict,settlement_edge

def structural_eligible(row:dict[str,Any],*,target_shares:float=20.0)->bool:
    target=int(round(target_shares*1_000_000))
    if target<=0:return False
    if row.get("confirmed_non_opposing") is not True:return False
    if abs(float(row.get("binance_return_100ms_bp") or 0.0))+1e-12<0.30:return False
    if not 0<=int(row.get("signal_age_ns") or -1)<=5_000_000_000:return False
    if not 5_000_000_000<=int(row.get("tte_ns") or -1)<=120_000_000_000:return False
    minimum=int(row.get("minimum_order_microunits") or 0)
    if minimum<=0 or minimum>target:return False
    if int(row.get("ask_quantity") or 0)<target:return False
    tick=int(row.get("tick_e4") or 0);ask=int(row.get("ask_e4") or 0)
    if tick<=0 or ask<=0 or ask%tick!=0:return False
    return 0<float(row.get("entry_price") or 0)<1

def _quantity(row:dict,max_loss_usd:float|None,target_shares:float)->float:
    quantity=float(target_shares)
    if max_loss_usd is None:return quantity
    risk=float(row["entry_price"])+float(row["fee_per_share"])
    if risk<=0:return 0.0
    return min(quantity,float(max_loss_usd)/risk)
def _trade(row:dict,probability:float,buffer:float,max_loss_usd:float|None,
           target_shares:float)->dict:
    edge=settlement_edge(probability,float(row["entry_price"]),
                         float(row["fee_per_share"]),float(buffer))
    q=_quantity(row,max_loss_usd,target_shares)
    pnl=q*(float(row["outcome"])-float(row["entry_price"])-float(row["fee_per_share"]))
    return {"market":row["market"],"asset":row["asset"],"horizon":row["horizon"],
            "decision_ns":row["decision_ns"],"probability":probability,
            "entry_price":row["entry_price"],"fee_per_share":row["fee_per_share"],
            "predicted_net_edge":edge,"shares":q,"outcome":row["outcome"],"net_pnl":pnl}

def _summary(trades:list[dict])->dict:
    if not trades:return {"trades":0,"wins":0,"win_rate":None,"net_pnl":0.0,
        "mean_pnl":None,"mean_predicted_edge":None,"mean_entry_price":None}
    return {"trades":len(trades),"wins":sum(t["outcome"]==1 for t in trades),
        "win_rate":sum(t["outcome"]==1 for t in trades)/len(trades),
        "net_pnl":sum(t["net_pnl"] for t in trades),
        "mean_pnl":mean(t["net_pnl"] for t in trades),
        "mean_predicted_edge":mean(t["predicted_net_edge"] for t in trades),
        "mean_entry_price":mean(t["entry_price"] for t in trades)}

def _weighted_brier(rows:list[dict],probabilities:list[float])->float|None:
    if not rows:return None
    counts={}
    for row in rows:counts[row["market"]]=counts.get(row["market"],0)+1
    weights=[1/counts[r["market"]] for r in rows];den=sum(weights)
    return sum(w*(p-float(r["outcome"]))**2 for r,p,w in zip(rows,probabilities,weights))/den
def evaluate(model:dict,rows:list[dict],*,edge_buffer:float,
             target_shares:float=20.0,max_loss_usd:float|None=None)->dict:
    heldout=[r for r in rows if int(r["decision_ns"])>=int(model["train_end_ns"])]
    heldout.sort(key=lambda r:(r["decision_ns"],r["market"],r["signal_version"]))
    probabilities=predict(model,heldout) if heldout else []
    by_market=defaultdict(list)
    for row,p in zip(heldout,probabilities):by_market[row["market"]].append((row,p))
    baseline=[];candidate=[]
    for market,events in by_market.items():
        eligible=[(r,p) for r,p in events if structural_eligible(r,target_shares=target_shares)]
        if not eligible:continue
        row,p=eligible[0]
        baseline.append(_trade(row,p,0.0,max_loss_usd,target_shares))
        for row,p in eligible:
            if settlement_edge(p,float(row["entry_price"]),float(row["fee_per_share"]),edge_buffer)>0:
                candidate.append(_trade(row,p,edge_buffer,max_loss_usd,target_shares));break
    prior=[float(r["pm_probability"]) for r in heldout]
    return {"schema":"polymarket_ev_policy_heldout_replay_v1","paper_only":True,
        "execution_authority":False,"automatic_promotion":False,
        "edge_buffer":edge_buffer,"target_shares":target_shares,"max_loss_usd":max_loss_usd,
        "heldout_rows":len(heldout),"heldout_markets":len(by_market),
        "model_brier_equal_market_weight":_weighted_brier(heldout,probabilities),
        "pm_prior_brier_equal_market_weight":_weighted_brier(heldout,prior),
        "baseline_first_structural":_summary(baseline),"ev_first_positive":_summary(candidate),
        "incremental_net_pnl":sum(t["net_pnl"] for t in candidate)-sum(t["net_pnl"] for t in baseline),
        "candidate_trades":candidate,
        "limitations":["Held-out markets, not rows, are the effective economic units.",
            "Depth is checked at the observed decision snapshot; no extra impact model is added here.",
            "No threshold is promoted automatically from this report."]}
