#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import v6_typed_structural as base
    from v6_market_common import finite
except ModuleNotFoundError:
    from scripts import v6_typed_structural as base
    from scripts.v6_market_common import finite


def atomic_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    with tmp.open("w",newline="",encoding="utf-8") as handle:
        writer=csv.DictWriter(handle,fieldnames=base.FIELDS); writer.writeheader(); writer.writerows(rows)
    os.replace(tmp,path)


def main() -> int:
    parser=argparse.ArgumentParser(description="Typed threshold payoff relations with explicit expiry identity"); parser.add_argument("--config",type=Path,default=Path("config/paper_v6.json")); parser.add_argument("--output",type=Path,required=True); parser.add_argument("--status",type=Path,required=True); parser.add_argument("--markets",type=int,default=700); parser.add_argument("--min-liquidity",type=float,default=10.0); parser.add_argument("--min-edge",type=float,default=0.0002); parser.add_argument("--max-trade-usd",type=float,default=60.0); args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding="utf-8")); gamma,clob=cfg["gamma_url"],cfg["clob_url"]; now=int(time.time()); markets=base.discover(gamma,args.markets,args.min_liquidity); families=defaultdict(list); parsed=expiry_unverified=0
    for market in markets:
        signature=base.threshold_signature(market.question)
        if signature is None: continue
        parsed+=1
        if market.end_ts<=0:
            expiry_unverified+=1; continue
        families[(signature.family,signature.direction,signature.unit,market.end_ts)].append((signature.threshold,market))
    selected={m.market_id:m for values in families.values() for _,m in values if len(values)>=2}; tokens=[token for market in selected.values() for token in (market.yes,market.no)]; books=base.fetch_books(clob,tokens) if tokens else {}; rows=[]; relations=bundles=0
    for (family,direction,unit,end_ts),values in families.items():
        values.sort(key=lambda item:item[0])
        if len(values)<2: continue
        for (low_threshold,low),(high_threshold,high) in zip(values,values[1:]):
            if high_threshold<=low_threshold: continue
            relations+=1; token_legs=[(low,"YES",low.yes),(high,"NO",high.no)] if direction=="UP" else [(high,"YES",high.yes),(low,"NO",low.no)]
            if any(token not in books for _,_,token in token_legs): continue
            leg_books=[books[token] for _,_,token in token_legs]; cost=sum(book.bid for book in leg_books); edge=1.0-cost
            if edge<=args.min_edge: continue
            min_shares=min(book.bid_size for book in leg_books); min_order=max(book.min_order for book in leg_books)
            if min_shares+1e-12<min_order: continue
            max_notional=min(args.max_trade_usd,min_shares*max(cost,1e-9))
            if max_notional<=0: continue
            identity=hashlib.sha256(f"{family}|{direction}|{unit}|{end_ts}|{low_threshold}|{high_threshold}".encode()).hexdigest()[:16]; bundle_id=f"STRUCTURAL_TYPED-{now}-{identity}"; hold=max(now+3600,end_ts+3600)
            for market,side,token in token_legs:
                rows.append({"bundle_id":bundle_id,"strategy":"STRUCTURAL_TYPED","event_id":"STRUCT_TYPED:"+identity,"created_ts":now,"mode":"MAKER","expected_edge":edge,"max_notional":max_notional,"market_id":market.market_id,"side":side,"weight":1.0,"limit_price":books[token].bid,"execution_deadline_ts":now+180,"hold_deadline_ts":hold})
            bundles+=1
    atomic_csv(args.output,rows); status={"timestamp":now,"paper_only":True,"markets":len(markets),"parsed_markets":parsed,"expiry_unverified":expiry_unverified,"typed_families":sum(len(values)>=2 for values in families.values()),"relations_considered":relations,"bundles":bundles,"rows":len(rows),"best_edge":max((finite(row.get("expected_edge"),0.0) for row in rows),default=0.0),"parser_contract":"identical text outside threshold/direction + same verified end_ts + same unit","strategy":"STRUCTURAL_TYPED"}; args.status.parent.mkdir(parents=True,exist_ok=True); tmp=args.status.with_suffix(args.status.suffix+".tmp"); tmp.write_text(json.dumps(status,indent=2,sort_keys=True)+"\n",encoding="utf-8"); os.replace(tmp,args.status); print(json.dumps(status,sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
