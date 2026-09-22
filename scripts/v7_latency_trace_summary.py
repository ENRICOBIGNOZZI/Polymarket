#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

MAGIC=0x3154434152543756
HEADER=struct.Struct("<QII")
RECORD=struct.Struct("<IIQQQQqqqqqqqqqq")
FIELDS=(
    "version","valid_mask","trace_id","market_handle","instrument_handle",
    "client_order_id","frame_receive","decode_complete","arb_decision",
    "risk_admitted","sign_start","sign_done","wire_start","wire_complete",
    "http_ack","user_ws_match",
)
SEGMENTS=(
    ("frame_receive_to_decode","frame_receive","decode_complete"),
    ("decode_to_arb_decision","decode_complete","arb_decision"),
    ("arb_decision_to_risk","arb_decision","risk_admitted"),
    ("risk_to_sign_start","risk_admitted","sign_start"),
    ("sign","sign_start","sign_done"),
    ("sign_done_to_wire_start","sign_done","wire_start"),
    ("wire_write","wire_start","wire_complete"),
    ("wire_complete_to_http_ack","wire_complete","http_ack"),
    ("http_ack_to_user_ws_match","http_ack","user_ws_match"),
    ("frame_receive_to_wire_start","frame_receive","wire_start"),
    ("frame_receive_to_http_ack","frame_receive","http_ack"),
    ("frame_receive_to_user_ws_match","frame_receive","user_ws_match"),
)

def quantile(values:list[int],p:float)->int|None:
    if not values:return None
    values=sorted(values)
    return values[int(round(max(0.0,min(1.0,p))*(len(values)-1)))]

def dist(values:list[int])->dict[str,int|None]:
    return {"count":len(values),"p50":quantile(values,.50),"p95":quantile(values,.95),
            "p99":quantile(values,.99),"p999":quantile(values,.999),"max":quantile(values,1.0)}

def read(path:Path)->list[dict[str,int]]:
    raw=path.read_bytes()
    if len(raw)<HEADER.size:raise ValueError("trace_header_missing")
    magic,version,record_size=HEADER.unpack_from(raw,0)
    if magic!=MAGIC or version!=1 or record_size!=RECORD.size:
        raise ValueError("trace_header_invalid")
    body=raw[HEADER.size:]
    if len(body)%RECORD.size:raise ValueError("trace_truncated")
    rows=[]
    for off in range(0,len(body),RECORD.size):
        values=RECORD.unpack_from(body,off)
        row=dict(zip(FIELDS,values))
        if row["version"]!=1 or row["trace_id"]<=0:raise ValueError("trace_record_invalid")
        rows.append(row)
    return rows

def coalesce(rows:list[dict[str,int]])->list[dict[str,int]]:
    merged:dict[tuple[int,int],dict[str,int]]={}
    for row in rows:
        trace_id=row["trace_id"]
        instrument=row["instrument_handle"]
        key=(trace_id,instrument)
        current=merged.setdefault(key,{field:0 for field in FIELDS})
        current["version"]=1
        current["trace_id"]=trace_id
        current["instrument_handle"]=instrument
        current["valid_mask"] |= row["valid_mask"]
        for key in FIELDS:
            if key in {"version","valid_mask","trace_id"}:continue
            value=row[key]
            if value<=0:continue
            prior=current[key]
            if key.endswith("_monotonic_ns") or key in {
                "frame_receive","decode_complete","arb_decision","risk_admitted",
                "sign_start","sign_done","wire_start","wire_complete","http_ack",
                "user_ws_match",
            }:
                if prior==0 or value<prior:current[key]=value
            elif prior==0:
                current[key]=value
    return [merged[key] for key in sorted(merged)]

def summarize(rows:list[dict[str,int]])->dict:
    traces=coalesce(rows)
    samples={name:[] for name,_,_ in SEGMENTS}
    for row in traces:
        for name,start,end in SEGMENTS:
            a,b=row[start],row[end]
            if a>0 and b>=a:samples[name].append(b-a)
    return {"schema":"polymarket_v7_latency_trace_summary_v1",
            "record_count":len(rows),"trace_count":len(traces),
            "units":"nanoseconds","segments":{name:dist(samples[name]) for name,_,_ in SEGMENTS}}

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("trace",type=Path)
    ap.add_argument("--output",type=Path)
    a=ap.parse_args()
    out=summarize(read(a.trace))
    text=json.dumps(out,sort_keys=True,indent=2)+"\n"
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(text,encoding="utf-8")
    else:print(text,end="")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
