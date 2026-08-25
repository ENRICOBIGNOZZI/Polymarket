#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def f(value: Any, default: float = 0.0) -> float:
    try: out=float(value)
    except (TypeError,ValueError,OverflowError): return default
    return out if math.isfinite(out) else default


def read_json(path: Path) -> dict[str, Any]:
    try:
        value=json.loads(path.read_text(encoding="utf-8")); return value if isinstance(value,dict) else {}
    except (OSError,json.JSONDecodeError): return {}


def csv_rows(path: Path) -> list[dict[str,str]]:
    try:
        with path.open(newline="",encoding="utf-8") as handle: return [dict(row) for row in csv.DictReader(handle)]
    except OSError: return []


def last_csv(path: Path) -> dict[str,str]:
    rows=csv_rows(path); return rows[-1] if rows else {}


def age(path: Path, now: int) -> float:
    try: return max(0.0,now-path.stat().st_mtime)
    except OSError: return 1e9


def atomic_json(path: Path, value: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8"); os.replace(tmp,path)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str,Any]]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    with tmp.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader(); writer.writerows([{key:row.get(key,"") for key in fields} for row in rows])
    os.replace(tmp,path)


def count_actions(path: Path, field: str = "action") -> Counter[str]:
    return Counter(str(row.get(field) or "").upper() for row in csv_rows(path))


def realized_from_ledger(rows: list[dict[str,str]]) -> float:
    return sum(f(row.get("net_pnl")) for row in rows)


def broker_breakdown(ledger: list[dict[str,str]], bundles: list[dict[str,str]]) -> dict[str,dict[str,Any]]:
    output: dict[str,dict[str,Any]]=defaultdict(lambda:{"realized_pnl":0.0,"finalized_bundles":0,"live_bundles":0})
    for row in ledger:
        strategy=str(row.get("strategy") or "UNKNOWN").upper(); output[strategy]["realized_pnl"]+=f(row.get("net_pnl")); output[strategy]["finalized_bundles"]+=1
    for row in bundles:
        status=str(row.get("status") or "").upper()
        if status in {"RESTING","COMPLETE","ABORTING"}: output[str(row.get("strategy") or "UNKNOWN").upper()]["live_bundles"]+=1
    return dict(sorted(output.items()))


def maker_orders(root: Path) -> list[dict[str,Any]]:
    status=read_json(root/"maker"/"status.json"); output=[]
    for market_id,row in (status.get("orders") or {}).items():
        if not isinstance(row,dict): continue
        output.append({"model":"micro_maker","strategy":"MICRO_MAKER","bundle_id":"","market_id":market_id,"side":row.get("side"),"state":"RESTING","limit_price":f(row.get("limit_price")),"remaining_shares":f(row.get("remaining_shares")),"queue_ahead":f(row.get("queue_ahead"))})
    return output


def broker_orders(root: Path) -> list[dict[str,Any]]:
    bundle_strategy={str(row.get("bundle_id") or ""):str(row.get("strategy") or "RELATIVE_VALUE") for row in csv_rows(root/"multileg_bundles.csv")}; output=[]
    for row in csv_rows(root/"multileg_legs.csv"):
        state=str(row.get("order_state") or "").upper()
        if state not in {"RESTING","CANCEL_PENDING"}: continue
        target=f(row.get("target_shares")); filled=f(row.get("filled_shares")); bundle=str(row.get("bundle_id") or "")
        output.append({"model":"relative_value","strategy":bundle_strategy.get(bundle,"RELATIVE_VALUE"),"bundle_id":bundle,"market_id":row.get("market_id"),"side":row.get("side"),"state":state,"limit_price":f(row.get("limit_price")),"remaining_shares":max(0.0,target-filled),"queue_ahead":f(row.get("queue_ahead"))})
    return output


def recent_fills(root: Path, limit: int = 60) -> list[dict[str,Any]]:
    output=[]
    for model,strategy,path in (
        ("micro_maker","MICRO_MAKER",root/"maker"/"maker_fills.csv"),
        ("micro_taker","MICRO_TAKER",root/"micro_taker"/"fills.csv"),
        ("graph_hard","GRAPH_HARD",root/"hard_arb"/"fills.csv"),
    ):
        for row in csv_rows(path)[-limit:]:
            output.append({"timestamp":int(f(row.get("timestamp"))),"model":model,"strategy":strategy,"market_id":row.get("market_id") or row.get("event_id") or "","side":row.get("side") or "","action":row.get("action") or "","shares":f(row.get("shares")),"price":f(row.get("price")),"pnl":f(row.get("pnl"))})
    bundle_strategy={str(row.get("bundle_id") or ""):str(row.get("strategy") or "RELATIVE_VALUE") for row in csv_rows(root/"multileg_bundles.csv")}
    for row in csv_rows(root/"multileg_events.csv")[-200:]:
        event=str(row.get("event") or "").upper()
        if "FILL" not in event and "UNWIND" not in event and "EXIT" not in event: continue
        bundle=str(row.get("bundle_id") or "")
        output.append({"timestamp":int(f(row.get("timestamp"))),"model":"relative_value","strategy":bundle_strategy.get(bundle,"RELATIVE_VALUE"),"market_id":row.get("market_id") or "","side":row.get("side") or "","action":event,"shares":f(row.get("shares")),"price":f(row.get("price")),"pnl":0.0})
    output.sort(key=lambda row:row["timestamp"],reverse=True); return output[:limit]


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--config",type=Path,default=Path("config/paper_v6.json")); parser.add_argument("--run-root",type=Path,required=True); args=parser.parse_args(); now=int(time.time()); cfg=read_json(args.config); v=cfg.get("v6") if isinstance(cfg.get("v6"),dict) else {}; starting=f(cfg.get("starting_capital"),10000.0); reserve_frac=f(v.get("reserve_fraction"),.05); reserve=starting*reserve_frac
    alloc={"micro_maker":starting*f(v.get("micro_maker_capital_fraction"),.12),"micro_taker":starting*f(v.get("micro_taker_capital_fraction"),.08),"relative_value":starting*f(v.get("relative_value_capital_fraction"),.50),"graph_hard":starting*f(v.get("hard_arb_capital_fraction"),.15),"external":starting*f(v.get("external_capital_fraction"),.10)}
    maker=read_json(args.run_root/"maker"/"status.json"); micro=read_json(args.run_root/"micro_taker"/"status.json"); broker=last_csv(args.run_root/"multileg_equity.csv"); hard=read_json(args.run_root/"hard_arb"/"status.json"); external=read_json(args.run_root/"external"/"status.json"); ledger=csv_rows(args.run_root/"bundle_ledger.csv"); bundles=csv_rows(args.run_root/"multileg_bundles.csv"); events=csv_rows(args.run_root/"multileg_events.csv")
    maker_eq=f(maker.get("equity"),alloc["micro_maker"]); micro_eq=f(micro.get("equity"),alloc["micro_taker"]); broker_eq=f(broker.get("equity"),alloc["relative_value"]); hard_eq=f(hard.get("equity"),alloc["graph_hard"]); external_eq=f(external.get("equity"),alloc["external"]); equity=reserve+maker_eq+micro_eq+broker_eq+hard_eq+external_eq; previous=read_json(args.run_root/"runtime_status.json"); peak=max(starting,f(previous.get("peak_equity"),starting),equity); drawdown=max(0.0,1.0-equity/peak) if peak else 0.0
    maker_actions=count_actions(args.run_root/"maker"/"maker_order_log.csv"); maker_fill_actions=count_actions(args.run_root/"maker"/"maker_fills.csv"); micro_fill_actions=count_actions(args.run_root/"micro_taker"/"fills.csv"); hard_fill_actions=count_actions(args.run_root/"hard_arb"/"fills.csv"); broker_event_actions=Counter(str(row.get("event") or "").upper() for row in events); broker_fills=sum(count for action,count in broker_event_actions.items() if "FILL" in action or "UNWIND" in action or "EXIT" in action); broker_realized=realized_from_ledger(ledger)
    maker_age=age(args.run_root/"maker"/"status.json",now); micro_age=age(args.run_root/"micro_taker"/"status.json",now); broker_age=age(args.run_root/"multileg_equity.csv",now); hard_age=age(args.run_root/"hard_arb"/"status.json",now); external_age=age(args.run_root/"external"/"status.json",now)
    strategies={
        "micro_maker":{"equity":maker_eq,"pnl":maker_eq-alloc["micro_maker"],"realized_pnl":f(maker.get("realized_pnl")),"gross_exposure":f(maker.get("gross_exposure")),"drawdown":f(maker.get("drawdown")),"live_units":int(f(maker.get("resting_orders")))+int(f(maker.get("open_positions"))),"orders_total":maker_actions.get("POST",0),"fills_total":sum(maker_fill_actions.values()),"status_age_seconds":maker_age,"killed":bool(maker.get("killed")),"signals":int(f(maker.get("signals"))),"best_edge":f(maker.get("best_edge"))},
        "micro_taker":{"equity":micro_eq,"pnl":micro_eq-alloc["micro_taker"],"realized_pnl":f(micro.get("realized_pnl_total"),f(micro.get("realized_pnl"))),"gross_exposure":f(micro.get("gross_exposure")),"drawdown":f(micro.get("drawdown")),"live_units":int(f(micro.get("open_positions"))),"orders_total":sum(micro_fill_actions.values()),"fills_total":sum(micro_fill_actions.values()),"status_age_seconds":micro_age,"killed":bool(micro.get("killed")),"signals":int(f(micro.get("signals"))),"best_edge":f(micro.get("best_edge")),"labeled_samples":int(f(micro.get("labeled_samples")))},
        "relative_value":{"equity":broker_eq,"pnl":broker_eq-alloc["relative_value"],"realized_pnl":broker_realized,"gross_exposure":f(broker.get("gross_entry_cash")),"drawdown":f(broker.get("drawdown")),"live_units":int(f(broker.get("live_bundles"))),"orders_total":broker_event_actions.get("POST",0),"fills_total":broker_fills,"status_age_seconds":broker_age,"killed":bool(int(f(broker.get("killed"))))},
        "graph_hard":{"equity":hard_eq,"pnl":hard_eq-alloc["graph_hard"],"realized_pnl":f(hard.get("realized_pnl")),"gross_exposure":f(hard.get("gross_exposure")),"drawdown":f(hard.get("drawdown")),"live_units":int(f(hard.get("open_positions"))),"orders_total":hard_fill_actions.get("BUY_COMPLETE_YES_SET_VWAP",0),"fills_total":sum(hard_fill_actions.values()),"status_age_seconds":hard_age,"killed":bool(hard.get("killed")),"signals":int(f(hard.get("positive_candidates"))),"best_edge":f(hard.get("best_edge"))},
        "external":{"equity":external_eq,"pnl":external_eq-alloc["external"],"realized_pnl":f(external.get("realized_pnl")),"gross_exposure":f(external.get("gross_exposure")),"drawdown":f(external.get("drawdown")),"live_units":int(f(external.get("open_positions"))),"orders_total":0,"fills_total":0,"status_age_seconds":external_age,"killed":bool(external.get("killed"))},
    }
    realized=sum(f(row.get("realized_pnl")) for row in strategies.values()); open_orders=(maker_orders(args.run_root)+broker_orders(args.run_root))[:100]; fills=recent_fills(args.run_root,60); killed=any(bool(row.get("killed")) for row in strategies.values()) or drawdown>=f(cfg.get("max_drawdown"),.15); gross=sum(f(row.get("gross_exposure")) for row in strategies.values()); reserved=reserve+f(maker.get("reserved_cash"))+f(broker.get("reserved_cash")); cash=reserve+f(maker.get("cash"),alloc["micro_maker"])+f(micro.get("cash"),alloc["micro_taker"])+f(broker.get("cash"),alloc["relative_value"])+f(hard.get("cash"),alloc["graph_hard"])+f(external.get("cash"),alloc["external"])
    status={"schema":"polymarket_v6_runtime_status_v2","timestamp":now,"version":6,"paper_only":True,"starting_capital":starting,"cash":cash,"equity":equity,"peak_equity":peak,"pnl":equity-starting,"realized_pnl":realized,"drawdown":drawdown,"killed":killed,"live_units":sum(int(row.get("live_units",0)) for row in strategies.values()),"reserved_cash":reserved,"gross_exposure":gross,"execution_imbalance":0.0,"execution_staleness":max(row["status_age_seconds"] for row in strategies.values()),"open_order_count":len(open_orders),"fill_count_total":sum(int(row.get("fills_total",0)) for row in strategies.values()),"strategies":strategies,"sub_strategies":broker_breakdown(ledger,bundles),"open_orders":open_orders,"recent_fills":fills,"relations":read_json(args.run_root/"relation_status.json"),"relation_guard":read_json(args.run_root/"relation_guard_status.json"),"queue_filter":read_json(args.run_root/"queue_filter_status.json"),"local_factor":read_json(args.run_root/"local_factor_status.json"),"typed_structural":read_json(args.run_root/"typed_structural_status.json"),"external_bridge":read_json(args.run_root/"external_bridge_status.json")}; atomic_json(args.run_root/"runtime_status.json",status)
    fields=["name","expert","capital_fraction","starting_capital","cash","equity","pnl","realized_pnl","peak_equity","drawdown","gross_exposure","open_positions","killed","alive","status_age_seconds","restarts","fills","buy_fills","sell_fills","settle_fills"]; rows=[]
    fractions={"micro_maker":f(v.get("micro_maker_capital_fraction"),.12),"micro_taker":f(v.get("micro_taker_capital_fraction"),.08),"relative_value":f(v.get("relative_value_capital_fraction"),.50),"graph_hard":f(v.get("hard_arb_capital_fraction"),.15),"external":f(v.get("external_capital_fraction"),.10)}
    for name,row in strategies.items():
        fraction=fractions[name]; s=starting*fraction; rows.append({"name":name,"expert":name,"capital_fraction":fraction,"starting_capital":s,"cash":row["equity"],"equity":row["equity"],"pnl":row["pnl"],"realized_pnl":row["realized_pnl"],"peak_equity":max(s,row["equity"]),"drawdown":row["drawdown"],"gross_exposure":row["gross_exposure"],"open_positions":row["live_units"],"killed":1 if row["killed"] else 0,"alive":1 if row["status_age_seconds"]<=180 else 0,"status_age_seconds":row["status_age_seconds"],"restarts":0,"fills":row["fills_total"],"buy_fills":0,"sell_fills":0,"settle_fills":0})
    atomic_csv(args.run_root/"strategy_status.csv",fields,rows); alive=sum(row["status_age_seconds"]<=180 for row in strategies.values()); atomic_json(args.run_root/"allocator_status.json",{"schema":"v6_health_view_v2","paper_only":True,"models_expected":5,"models_alive":alive,"reserve_fraction":reserve_frac,"global_max_drawdown":f(cfg.get("max_drawdown"),.15),"global_max_gross_fraction":f(cfg.get("max_gross_fraction"),.45),"global_gross_fraction":gross/max(starting,1.0),"timestamp":now}); print(json.dumps({key:status[key] for key in ("equity","pnl","realized_pnl","drawdown","open_order_count","fill_count_total","killed")},sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
