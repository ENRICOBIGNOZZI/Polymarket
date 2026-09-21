"""All-crypto executable timing equities from continuous PM book evidence.

PAPER-only research. Reuses the BTC continuous-tape execution semantics and
produces aggregate, asset, contract-horizon and asset×contract equity paths.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from research.walk_forward_v2.core import SAFETY, atomic_json, build_dataset
from research.walk_forward_v3.direct_action import _valid_state
from research.walk_forward_v3.btc_compact_equity import (
    LATENCIES, EXITS, SIZE, market_halves, stream_sessions, jsonl_sessions,
    session_for_row, execute_cell,
)

SCHEMA="polymarket_v7_all_crypto_compact_timing_equity_v1"


def fresh():
    return {"actions":0,"fills":0,"pnl":0.0,"positive":0,"negative":0,"zero":0}


def add(stats,economics):
    stats["actions"]+=1
    pnl=float(economics["cash_pnl"])
    stats["pnl"]+=pnl
    if float(economics.get("filled") or 0)>0:
        stats["fills"]+=1
    if pnl>1e-15: stats["positive"]+=1
    elif pnl<-1e-15: stats["negative"]+=1
    else: stats["zero"]+=1


def finish(stats):
    out=dict(stats)
    out["fill_rate"]=stats["fills"]/stats["actions"] if stats["actions"] else None
    out["pnl_per_fill"]=stats["pnl"]/stats["fills"] if stats["fills"] else None
    return out


def analyze(root,minimum_wall_ns):
    data=build_dataset(root,minimum_wall_ns=minimum_wall_ns,
                       include_settlement_labels=False,use_compact_window_index=True)
    if data.get("input_state")!="READY":
        return {"schema":SCHEMA,**SAFETY,"state":data.get("input_state")}

    rows=[r for r in data["decisions"] if _valid_state(r)]
    assets=sorted({str(r.get("asset") or "UNKNOWN") for r in rows})
    contracts=sorted({str(r.get("horizon") or "UNKNOWN") for r in rows})

    sessions,tape_diag=stream_sessions(Path(root).resolve().parent,rows)
    if not sessions:
        sessions,jsonl_diag=jsonl_sessions(Path(root).resolve(),rows)
        tape_diag={**tape_diag,**jsonl_diag,"fallback":"JSONL_BOOK_OBSERVATIONS"}

    discovery,validation=market_halves(rows)
    session_cache={r["decision_id"]:session_for_row(sessions,r) for r in rows}

    out={"schema":SCHEMA,**SAFETY,"state":"READY","diagnostic_only":True,
         "automatic_promotion":False,"latencies_ms":list(LATENCIES),
         "exit_horizons_ms":list(EXITS),"target_size_shares":SIZE,
         "assets":assets,"contract_horizons":contracts,
         "data_sha256":data.get("data_sha256"),"tape_diagnostics":tape_diag,
         "splits":{}}

    for split_name,markets in (("DISCOVERY",discovery),("VALIDATION",validation)):
        split_rows=[r for r in rows if str(r["market_id"]) in markets]
        tables={
            "overall":defaultdict(fresh),
            "by_asset":defaultdict(lambda:defaultdict(fresh)),
            "by_contract_horizon":defaultdict(lambda:defaultdict(fresh)),
            "by_asset_contract":defaultdict(lambda:defaultdict(fresh)),
        }
        censored=defaultdict(lambda:defaultdict(int))
        events=defaultdict(list)

        for row in split_rows:
            asset=str(row.get("asset") or "UNKNOWN")
            contract=str(row.get("horizon") or "UNKNOWN")
            session,session_reason=session_cache[row["decision_id"]]
            for latency in LATENCIES:
                for exit_ms in EXITS:
                    key=f"{latency}::{exit_ms}"
                    if session is None:
                        censored[key][session_reason]+=1
                        continue
                    economics,state=execute_cell(row,session,latency,exit_ms)
                    if economics is None:
                        censored[key][state]+=1
                        continue
                    add(tables["overall"][key],economics)
                    add(tables["by_asset"][asset][key],economics)
                    add(tables["by_contract_horizon"][contract][key],economics)
                    add(tables["by_asset_contract"][f"{asset}::{contract}"][key],economics)
                    if float(economics.get("filled") or 0)>0:
                        events[key].append({
                            "decision_ns":int(row["decision_ns"]),
                            "decision_id":str(row["decision_id"]),
                            "market_id":str(row["market_id"]),
                            "asset":asset,
                            "contract_horizon":contract,
                            "cash_pnl":float(economics["cash_pnl"]),
                            "execution_state":state,
                            **economics,
                        })

        for seq in events.values():
            seq.sort(key=lambda e:(e["decision_ns"],e["decision_id"]))

        out["splits"][split_name]={
            "decision_rows":len(split_rows),
            "overall":{k:finish(v) for k,v in sorted(tables["overall"].items())},
            "by_asset":{
                g:{k:finish(v) for k,v in sorted(cells.items())}
                for g,cells in sorted(tables["by_asset"].items())
            },
            "by_contract_horizon":{
                g:{k:finish(v) for k,v in sorted(cells.items())}
                for g,cells in sorted(tables["by_contract_horizon"].items())
            },
            "by_asset_contract":{
                g:{k:finish(v) for k,v in sorted(cells.items())}
                for g,cells in sorted(tables["by_asset_contract"].items())
            },
            "equity_events":dict(sorted(events.items())),
            "censored":{k:dict(sorted(v.items())) for k,v in sorted(censored.items())},
        }
    return out


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    a=p.parse_args(argv)
    out=analyze(a.root,a.minimum_wall_ns)
    atomic_json(a.output,out)
    return 0 if out.get("state")=="READY" else 2


if __name__=="__main__":
    raise SystemExit(main())
