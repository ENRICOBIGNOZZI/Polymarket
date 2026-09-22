#!/usr/bin/env python3
"""Fail-closed Polymarket venue-mode policy for pure arbitrage.

This module never grants execution authority. It normalizes a small explicit
state machine used by PAPER execution shadows and, later, by any authenticated
adapter after separate authorization.

Unknown/stale mode => DEGRADED => no new risk.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any

SCHEMA="polymarket_v7_pure_arb_venue_mode_v1"
MODES={"NORMAL","POST_ONLY","CANCEL_ONLY","PAUSED","DEGRADED"}


def atomic_json(path: Path, value: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,path)


def policy(mode: str) -> dict[str,bool]:
    mode=str(mode or "").upper()
    if mode=="NORMAL":
        return {"new_taker":True,"new_maker":True,"cancel":True}
    if mode=="POST_ONLY":
        return {"new_taker":False,"new_maker":True,"cancel":True}
    if mode=="CANCEL_ONLY":
        return {"new_taker":False,"new_maker":False,"cancel":True}
    return {"new_taker":False,"new_maker":False,"cancel":True}


def resolve_mode(source: dict[str,Any], *, now_ms: int, maximum_age_ms: int) -> tuple[str,str]:
    if not isinstance(source,dict):
        return "DEGRADED","SOURCE_MISSING"
    raw=str(source.get("mode") or "").upper()
    try: ts=int(source.get("timestamp_ms") or 0)
    except (TypeError,ValueError): ts=0
    if raw not in MODES:
        return "DEGRADED","MODE_UNKNOWN"
    if ts<=0 or now_ms-ts<0 or now_ms-ts>maximum_age_ms:
        return "DEGRADED","MODE_STALE"
    return raw,"VERIFIED_EXPLICIT_MODE"


def build(source: dict[str,Any], *, model_sha: str, now_ms: int,
          maximum_age_ms: int=5000, paper_counterfactual_mode: str|None=None) -> dict[str,Any]:
    observed,reason=resolve_mode(source,now_ms=now_ms,maximum_age_ms=maximum_age_ms)
    simulation=observed
    counterfactual=False
    if paper_counterfactual_mode:
        requested=paper_counterfactual_mode.upper()
        if requested not in MODES:
            raise ValueError("invalid counterfactual mode")
        simulation=requested
        counterfactual=True
    return {
        "schema":SCHEMA,"version":1,"model_sha":model_sha,
        "timestamp_ms":now_ms,
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"real_capital_at_risk":False,
        "execution_authority":"ZERO_AUTHORITY_POLICY_ONLY",
        "observed_mode":observed,"observed_reason":reason,
        "observed_policy":policy(observed),
        "simulation_mode":simulation,"simulation_policy":policy(simulation),
        "paper_counterfactual":counterfactual,
        "unknown_or_stale_policy":"DEGRADED_NO_NEW_RISK",
    }


def load(path: Path|None) -> dict[str,Any]:
    if path is None:return {}
    try:v=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):return {}
    return v if isinstance(v,dict) else {}


def main()->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source",type=Path)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--model-sha",required=True)
    ap.add_argument("--maximum-age-ms",type=int,default=5000)
    ap.add_argument("--paper-counterfactual-mode",choices=sorted(MODES))
    ap.add_argument("--interval-ms",type=int,default=1000)
    args=ap.parse_args()
    if len(args.model_sha)!=40 or any(c not in "0123456789abcdef" for c in args.model_sha):
        raise SystemExit("invalid model sha")
    if not(100<=args.maximum_age_ms<=600_000 and 50<=args.interval_ms<=60_000):
        raise SystemExit("invalid timing")
    while True:
        atomic_json(args.output,build(
            load(args.source),model_sha=args.model_sha,
            now_ms=time.time_ns()//1_000_000,
            maximum_age_ms=args.maximum_age_ms,
            paper_counterfactual_mode=args.paper_counterfactual_mode))
        time.sleep(args.interval_ms/1000.0)


if __name__=="__main__":
    raise SystemExit(main())
