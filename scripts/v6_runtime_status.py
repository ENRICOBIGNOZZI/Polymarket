#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from pathlib import Path


def f(value, default=0.0):
    try: out = float(value)
    except (TypeError, ValueError): return default
    return out if math.isfinite(out) else default


def last_csv(path: Path) -> dict[str, str]:
    try:
        with path.open(newline="", encoding="utf-8") as h: rows = list(csv.DictReader(h))
        return rows[-1] if rows else {}
    except OSError: return {}


def read_json(path: Path) -> dict:
    try:
        x = json.loads(path.read_text(encoding="utf-8")); return x if isinstance(x, dict) else {}
    except (OSError, json.JSONDecodeError): return {}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"); os.replace(tmp, path)


def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--config",type=Path,default=Path("config/paper_v6.json")); ap.add_argument("--run-root",type=Path,required=True); args=ap.parse_args()
    cfg=read_json(args.config); v6=cfg.get("v6") if isinstance(cfg.get("v6"),dict) else {}; starting=f(cfg.get("starting_capital"),10000); reserve_frac=f(v6.get("reserve_fraction"),.05); reserve=starting*reserve_frac
    maker=last_csv(args.run_root/"maker"/"maker_equity.csv"); micro=read_json(args.run_root/"micro_taker"/"status.json"); broker=last_csv(args.run_root/"multileg_equity.csv"); external=read_json(args.run_root/"external"/"status.json")
    alloc={
        "micro_maker": starting*f(v6.get("micro_maker_capital_fraction"),.12),
        "micro_taker": starting*f(v6.get("micro_taker_capital_fraction"),.08),
        "relative_value": starting*f(v6.get("multileg_capital_fraction"),.65),
        "external": starting*f(v6.get("external_capital_fraction"),.10),
    }
    maker_eq=f(maker.get("equity"),alloc["micro_maker"]); micro_eq=f(micro.get("equity"),alloc["micro_taker"]); broker_eq=f(broker.get("equity"),alloc["relative_value"]); external_eq=f(external.get("equity"),alloc["external"])
    equity=reserve+maker_eq+micro_eq+broker_eq+external_eq; previous=read_json(args.run_root/"runtime_status.json"); peak=max(starting,f(previous.get("peak_equity"),starting),equity); drawdown=max(0.0,1.0-equity/peak) if peak>0 else 0.0
    local_killed=bool(int(f(maker.get("killed")))) or bool(micro.get("killed",False)) or bool(int(f(broker.get("killed")))) or bool(external.get("killed",False)); killed=local_killed or drawdown>=f(cfg.get("max_drawdown"),.15)
    maker_live=int(f(maker.get("resting_orders")))+int(f(maker.get("positions"))); micro_live=int(f(micro.get("open_positions"))); broker_live=int(f(broker.get("live_bundles"))); external_live=int(f(external.get("open_positions")))
    reserved=reserve+f(maker.get("reserved_cash"))+f(broker.get("reserved_cash")); gross=f(maker.get("reserved_cash"))+f(micro.get("gross_exposure"))+f(broker.get("gross_entry_cash"))+f(external.get("gross_exposure"))
    relations=read_json(args.run_root/"relation_status.json"); local_factor=read_json(args.run_root/"local_factor_status.json"); bridge=read_json(args.run_root/"external_bridge_status.json")
    strategies={
        "micro_maker":{"equity":maker_eq,"pnl":maker_eq-alloc["micro_maker"],"live_units":maker_live,"killed":bool(int(f(maker.get("killed"))))},
        "micro_taker":{"equity":micro_eq,"pnl":micro_eq-alloc["micro_taker"],"live_units":micro_live,"killed":bool(micro.get("killed",False)),"signals":int(f(micro.get("signals"))),"best_edge":f(micro.get("best_edge")),"labeled_samples":int(f(micro.get("labeled_samples")))},
        "local_factor_graph_structural":{"equity":broker_eq,"pnl":broker_eq-alloc["relative_value"],"live_units":broker_live,"killed":bool(int(f(broker.get("killed"))))},
        "external":{"equity":external_eq,"pnl":external_eq-alloc["external"],"live_units":external_live,"killed":bool(external.get("killed",False))},
    }
    status={
        "schema":"polymarket_v6_runtime_status_v1","timestamp":int(time.time()),"version":6,"paper_only":True,"starting_capital":starting,
        "cash":f(maker.get("cash"),alloc["micro_maker"])+f(micro.get("cash"),alloc["micro_taker"])+f(broker.get("cash"),alloc["relative_value"])+f(external.get("cash"),alloc["external"])+reserve,
        "equity":equity,"peak_equity":peak,"pnl":equity-starting,"drawdown":drawdown,"killed":killed,"live_units":maker_live+micro_live+broker_live+external_live,
        "reserved_cash":reserved,"gross_exposure":gross,"realized_pnl":f(micro.get("realized_pnl"))+f(external.get("realized_pnl")),"execution_imbalance":0.0,"execution_staleness":0.0,
        "strategies":strategies,"relations":relations,"local_factor":local_factor,"external_bridge":bridge,
    }
    atomic_json(args.run_root/"runtime_status.json",status)
    print(json.dumps({k:status[k] for k in ("equity","pnl","drawdown","live_units","killed")},sort_keys=True)); return 0


if __name__=="__main__": raise SystemExit(main())
