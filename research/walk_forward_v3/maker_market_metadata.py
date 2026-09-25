#!/usr/bin/env python3
"""Resolve static metadata for Polymarket markets present in causal PM tapes.

This is research metadata only. It never selects quote times and never reads
future returns or settlement outcomes. Market IDs come from the causal PM
observer manifests; Gamma is queried only for contract identity, token mapping,
contract times and fee/minimum terms.
"""
from __future__ import annotations

import argparse
import gzip
from datetime import datetime
import json
import math
from pathlib import Path
import re
import time
from typing import Any
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SCHEMA="polymarket_v7_maker_market_metadata_v1"
GAMMA="https://gamma-api.polymarket.com"
ET=ZoneInfo("America/New_York")


def load(path: Path) -> dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict):raise ValueError("JSON_OBJECT_REQUIRED")
    return value


def json_lines(path: Path):
    """Read newline-delimited JSON from plain or gzip segments by file magic."""
    try:
        with path.open("rb") as handle:
            magic=handle.read(2)
    except OSError:
        return
    opener=gzip.open if magic==b"\x1f\x8b" else open
    try:
        with opener(path,"rt",encoding="utf-8") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    continue
                try:
                    value=json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value,dict):
                    yield value
    except (OSError,UnicodeDecodeError):
        return


def finite(value):
    if value is None or isinstance(value,bool):return None
    try:x=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return x if math.isfinite(x) else None


def fetch_market(market_id: str, timeout=8) -> dict[str,Any]:
    urls=(
        GAMMA+"/markets/"+urllib.parse.quote(market_id,safe=""),
        GAMMA+"/markets?"+urllib.parse.urlencode({"id":market_id}),
    )
    last=None
    for url in urls:
        request=urllib.request.Request(url,headers={"User-Agent":"polymarket-v7-maker-research/1"})
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:
                value=json.loads(response.read().decode("utf-8"))
            if isinstance(value,list):
                value=next((x for x in value if isinstance(x,dict) and str(x.get("id") or "")==market_id),
                           next((x for x in value if isinstance(x,dict)),{}))
            elif isinstance(value,dict) and isinstance(value.get("markets"),list):
                value=next((x for x in value["markets"] if isinstance(x,dict) and str(x.get("id") or "")==market_id),
                           next((x for x in value["markets"] if isinstance(x,dict)),{}))
            if isinstance(value,dict) and str(value.get("id") or "")==market_id:
                return value
        except (OSError,TimeoutError,ValueError,json.JSONDecodeError) as exc:
            last=exc
    raise RuntimeError("GAMMA_MARKET_LOOKUP_FAILED:"+market_id+":"+type(last).__name__)


def array(value):
    if isinstance(value,list):return value
    if isinstance(value,str):
        try:
            parsed=json.loads(value)
            return parsed if isinstance(parsed,list) else []
        except json.JSONDecodeError:
            return []
    return []


def iso_unix(value: Any) -> int:
    text=str(value or "").strip()
    if not text:return 0
    try:return int(datetime.fromisoformat(text.replace("Z","+00:00")).timestamp())
    except ValueError:return 0


def contexts(registry: dict[str,Any]):
    rows=[]
    for row in registry.get("contexts") or []:
        if not isinstance(row,dict) or row.get("enabled") is not True:continue
        p=row.get("polymarket") if isinstance(row.get("polymarket"),dict) else {}
        rows.append({
            "asset":str(row.get("asset") or "").upper(),
            "horizon":str(row.get("horizon") or "").upper(),
            "horizon_seconds":int(row.get("horizon_seconds") or 0),
            "slug_kind":str(p.get("slug_kind") or ""),
            "slug_prefix":str(p.get("slug_prefix") or "").lower(),
            "horizon_slug":str(p.get("horizon_slug") or "").lower(),
        })
    return rows


def identify_context(slug: str, registry: dict[str,Any], raw: dict[str,Any]):
    slug=slug.lower()
    candidates=[]
    for ctx in contexts(registry):
        prefix=ctx["slug_prefix"]; kind=ctx["slug_kind"]; hs=ctx["horizon_slug"]
        matched=False;start=0
        if kind=="UNIX_WINDOW":
            m=re.fullmatch(re.escape(prefix)+r"-updown-"+re.escape(hs)+r"-(\d+)",slug)
            if m:
                matched=True;start=int(m.group(1))
        elif kind=="HOURLY_ET":
            matched=slug.startswith(prefix+"-up-or-down-") and "-up-or-down-on-" not in slug
        elif kind=="DAILY_ET":
            matched=slug.startswith(prefix+"-up-or-down-on-")
        if matched:
            candidates.append((ctx,start))
    if len(candidates)!=1:
        raise ValueError("AMBIGUOUS_OR_UNKNOWN_CONTEXT:"+slug)
    ctx,start=candidates[0]
    close=iso_unix(raw.get("endDate") or raw.get("end_date_iso"))
    if start<=0 and close>0:
        start=close-int(ctx["horizon_seconds"])
    if close<=0 and start>0:
        close=start+int(ctx["horizon_seconds"])
    if start<=0 or close<=start:
        raise ValueError("CONTRACT_TIME_UNAVAILABLE:"+slug)
    return ctx,start,close


def observed_markets(run_root: Path, minimum_wall_ns: int):
    """Find market IDs from compact manifests or, when absent, the raw causal book."""
    roots=(run_root,run_root.parent)
    found={}
    counts={"manifests":0,"status_rejected":0,"manifest_tokens":0,
            "raw_files":0,"raw_rows":0,"raw_accepted":0}
    for base in roots:
        if not base.exists():continue
        for path in base.rglob("*.manifest.json"):
            if path.is_symlink() or not path.is_file():continue
            try:value=load(path)
            except (OSError,ValueError,json.JSONDecodeError):continue
            if value.get("schema")!="polymarket_v7_compact_pm_label_tape_manifest_v2":continue
            sid=str(value.get("observer_session_id") or "")
            status=path.parent/(sid+".status.json")
            try:sv=load(status)
            except (OSError,ValueError,json.JSONDecodeError):
                counts["status_rejected"]+=1;continue
            watermark=int(sv.get("book_watermark_receive_wall_ms") or 0)
            if watermark*1_000_000<minimum_wall_ns:continue
            counts["manifests"]+=1
            for row in value.get("tokens") or []:
                if not isinstance(row,dict):continue
                market=str(row.get("market_id") or "");token=str(row.get("token_id") or "")
                outcome=str(row.get("outcome") or "").upper()
                if not market or not token or outcome not in ("YES","NO"):continue
                state=found.setdefault(market,{"market_id":market,"tokens":{}})
                prior=state["tokens"].get(outcome)
                if prior is not None and prior!=token:
                    raise ValueError("CONFLICTING_MANIFEST_TOKEN:"+market+":"+outcome)
                state["tokens"][outcome]=token
                counts["manifest_tokens"]+=1
    compact_found=bool(found)

    # Current London runtime does not enable compact-label output on the
    # repricing observer. Raw causal book rows remain the authoritative source
    # for observed market membership.
    candidates=[]
    data_root=run_root.parent.resolve()
    if data_root.exists():
        for directory in data_root.rglob("book_observations"):
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and "repricing_book" in directory.parts
            ):
                candidates.extend(directory.glob("*.jsonl*"))
        for directory in data_root.rglob("repricing-book"):
            if directory.is_dir() and not directory.is_symlink():
                candidates.extend(directory.glob("*.jsonl*"))
    paths=sorted({p.resolve() for p in candidates if p.is_file() and not p.is_symlink()})
    if not paths:
        counts["source"]="COMPACT_MANIFEST" if compact_found else "NONE"
        return found,counts
    seen=set()
    for path in paths:
        counts["raw_files"]+=1
        for raw in json_lines(path):
            counts["raw_rows"]+=1
            if raw.get("schema")!="polymarket_v7_causal_book_observation_v1":continue
            try:wall=int(raw.get("receive_wall_ms") or 0)
            except (TypeError,ValueError,OverflowError):continue
            if wall*1_000_000<minimum_wall_ns:continue
            if (
                raw.get("paper_only") is not True
                or raw.get("authenticated_execution") is not False
                or raw.get("real_order_submission") is not False
                or raw.get("execution_authority")!="ZERO_AUTHORITY_RESEARCH_ONLY"
            ):
                continue
            market=str(raw.get("market_id") or "")
            token=str(raw.get("token_id") or "")
            if not market or not token:continue
            event_key=(
                str(raw.get("model_sha") or ""),
                str(raw.get("observer_session_id") or ""),
                int(raw.get("connection_epoch") or 0),
                int(raw.get("observer_sequence") or 0),
                market,token,wall,
            )
            if event_key in seen:
                counts["raw_duplicates"] = counts.get("raw_duplicates",0)+1
                continue
            seen.add(event_key)
            state=found.setdefault(market,{"market_id":market,"tokens_seen":set(),"tokens":{}})
            state.setdefault("tokens_seen",set()).add(token)
            counts["raw_accepted"]+=1
    for state in found.values():
        state["tokens_seen"]=sorted(state.get("tokens_seen") or ())
    counts["source"]="COMPACT_MANIFEST_PLUS_RAW_CAUSAL_BOOK" if compact_found else "RAW_CAUSAL_BOOK"
    return found,counts


def gamma_metadata(raw: dict[str,Any], registry: dict[str,Any], manifest: dict[str,Any]):
    market_id=str(raw.get("id") or "")
    slug=str(raw.get("slug") or "")
    ctx,start,close=identify_context(slug,registry,raw)
    tokens=[str(x).strip() for x in array(raw.get("clobTokenIds")) if str(x).strip()]
    outcomes=[str(x).strip().upper() for x in array(raw.get("outcomes"))]
    mapping={}
    for i,outcome in enumerate(outcomes[:len(tokens)]):
        if outcome in ("YES","UP"):mapping["YES"]=tokens[i]
        if outcome in ("NO","DOWN"):mapping["NO"]=tokens[i]
    for outcome,token in manifest.get("tokens",{}).items():
        if outcome in mapping and mapping[outcome]!=token:
            raise ValueError("GAMMA_MANIFEST_TOKEN_MISMATCH:"+market_id+":"+outcome)
        mapping[outcome]=token
    if not mapping.get("YES") or not mapping.get("NO") or mapping["YES"]==mapping["NO"]:
        raise ValueError("TOKEN_MAPPING_UNAVAILABLE:"+market_id)
    schedule=raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"),dict) else {}
    fees_enabled=bool(raw.get("feesEnabled",False))
    rate=finite(schedule.get("rate"))
    exponent=finite(schedule.get("exponent"))
    if not fees_enabled:
        rate=0.0; exponent=1.0
    minimum=None
    for key in ("orderMinSize","order_min_size","minimumOrderSize","minimum_order_size"):
        value=finite(raw.get(key))
        if value is not None and value>=0:
            minimum=value;break
    return {
        "market_id":market_id,"asset":ctx["asset"],"horizon":ctx["horizon"],
        "horizon_seconds":ctx["horizon_seconds"],"slug":slug,
        "yes_token":mapping["YES"],"no_token":mapping["NO"],
        "start_timestamp_ms":start*1000,"end_timestamp_ms":close*1000,
        "fee_rate":rate,"fee_exponent":exponent,"minimum":minimum,
        "fee_schedule":schedule,"fees_enabled":fees_enabled,
    }


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-root",type=Path,required=True)
    p.add_argument("--registry",type=Path,required=True)
    p.add_argument("--minimum-wall-ns",type=int,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    registry=load(a.registry)
    manifests,diag=observed_markets(a.run_root,a.minimum_wall_ns)
    if not manifests:raise SystemExit("NO_OBSERVED_PM_MARKETS")
    rows=[];rejected={}
    for market_id in sorted(manifests):
        try:
            rows.append(gamma_metadata(fetch_market(market_id),registry,manifests[market_id]))
        except Exception as exc:
            rejected[market_id]=type(exc).__name__+":"+str(exc)
    if not rows:raise SystemExit("NO_MARKET_METADATA")
    payload={
        "schema":SCHEMA,"version":1,
        "paper_only":True,"authenticated_execution":False,
        "real_order_submission":False,"execution_authority":False,
        "minimum_wall_ns":a.minimum_wall_ns,
        "generated_at_ns":time.time_ns(),
        "manifest_diagnostics":diag,
        "market_count":len(rows),"markets":rows,
        "rejected":rejected,
        "semantics":"STATIC_CONTRACT_METADATA_ONLY;NO_OUTCOME_OR_PNL_USED_FOR_ANCHORS",
    }
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(payload,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"market_count":len(rows),"rejected":len(rejected)},sort_keys=True))


if __name__=="__main__":
    main()
