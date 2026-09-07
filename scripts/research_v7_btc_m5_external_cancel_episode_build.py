#!/usr/bin/env python3
import argparse,bisect,csv,gzip,hashlib,json,math,shutil,subprocess,tempfile
from contextlib import contextmanager
from pathlib import Path

RULE_SHA="9e8c7e6a1d7e4a87cd9977396bcbbb228f96b4e35e4a34e84e1514e9e9630254"
EPISODE_SCHEMA="polymarket_v7_btc_m5_external_cancel_forward_episode_v2"
PROTOCOL_SCHEMA="polymarket_v7_btc_m5_external_cancel_episode_protocol_v2"


def run_csv(cmd):
    p=subprocess.Popen(cmd,stdout=subprocess.PIPE,text=True,bufsize=1)
    try:
        for row in csv.DictReader(p.stdout):
            yield row
    finally:
        if p.stdout:
            p.stdout.close()
        rc=p.wait()
        if rc:
            raise RuntimeError(f"decoder_failed:{cmd[0]}:{rc}")


@contextmanager
def materialized(path: Path):
    if path.suffix != ".gz":
        yield path
        return
    with tempfile.NamedTemporaryFile(suffix=".bin") as tmp:
        with gzip.open(path,"rb") as src:
            shutil.copyfileobj(src,tmp,length=4*1024*1024)
        tmp.flush()
        yield Path(tmp.name)


def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(4*1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def step_le(ts,vals,t):
    i=bisect.bisect_right(ts,t)-1
    return vals[i] if i>=0 else None


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--root",type=Path,required=True)
    ap.add_argument("--market",required=True)
    ap.add_argument("--protocol",type=Path,required=True)
    ap.add_argument("--external-tape",type=Path,nargs="+",required=True)
    ap.add_argument("--tape-dump",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--summary",type=Path,required=True)
    a=ap.parse_args()

    protocol=json.loads(a.protocol.read_text())
    if protocol.get("schema")!=PROTOCOL_SCHEMA or protocol.get("episode_schema")!=EPISODE_SCHEMA:
        raise SystemExit("protocol_schema")
    if protocol.get("canonical_rule_sha256")!=RULE_SHA:
        raise SystemExit("unexpected_rule_hash")
    rule=protocol["canonical_rule"]
    frozen=(rule["shock_source"],rule["shock_window_ms"],rule["minimum_absolute_log_return_bp"],
            rule["confirmation_source"],rule["trigger_cooldown_ms"],rule["baseline_action"],
            rule["queue_ahead_multiplier"],rule["cancel_latency_ms"])
    if frozen!=("BINANCE_SPOT_TRADES",100,0.3,"COINBASE_SPOT_TOP_OF_BOOK",250,"JOIN",1.5,100):
        raise SystemExit("rule_drift")

    manifests=[]
    for p in a.root.glob(f"btc-m5-book.{a.market}.*.manifest.json"):
        manifests.append((p,json.loads(p.read_text())))
    if len(manifests)!=1:
        raise SystemExit(f"manifest_count:{len(manifests)}")
    mp,manifest=manifests[0]
    if int(manifest.get("payload_schema_version",0))!=2:
        raise SystemExit("book_schema")
    segs=sorted((a.root/"normalized_events").glob(f"btc-m5-book.{a.market}.*.bin"))
    if not segs:
        raise SystemExit("no_closed_book_segments")
    if list((a.root/"normalized_events").glob(f"btc-m5-book.{a.market}.*.bin.open")):
        raise SystemExit("market_not_closed")

    latest={};snaps=[];trades=[];seq_prev=0;wall_prev=0;causal_viol=[];book_events=0
    for r in run_csv([str(a.tape_dump),"--book",*[str(x) for x in segs]]):
        seq=int(r["seq"]);wall_ns=int(r["receive_ms"])*1_000_000
        outcome=int(r["outcome"]);kind=int(r["kind"])
        if seq<=seq_prev:
            causal_viol.append(f"book_sequence_nonmonotone:{seq_prev}->{seq}")
        if wall_prev and wall_ns<wall_prev:
            causal_viol.append(f"book_wall_time_regressed:{wall_prev}->{wall_ns}")
        seq_prev=seq;wall_prev=max(wall_prev,wall_ns)
        if kind==4:
            causal_viol.append(f"book_lineage_invalidated:{seq}")
        elif kind==1:
            bid=float(r["bid"]);ask=float(r["ask"]);bq=float(r["bidq"]);aq=float(r["askq"])
            if outcome in (1,2) and 0<bid<ask<1 and bq>=0 and aq>=0:
                latest[outcome]=(bid,ask,bq,aq);book_events+=1
                if 1 in latest and 2 in latest:
                    snaps.append((wall_ns,latest[1],latest[2]))
        elif kind==2:
            px=float(r["trade_price"]);qty=float(r["trade_qty"]);side=int(r["trade_side"])
            if outcome in (1,2) and px>0 and qty>0 and side in (-1,1):
                trades.append((wall_ns,outcome,side,px,qty))
    if not snaps:
        raise SystemExit("missing_complement_consistent_book")
    trades.sort(key=lambda x:x[0])
    st=[x[0] for x in snaps]

    binance=[];coinbase=[]
    for tape in a.external_tape:
        with materialized(tape) as native:
            for r in run_csv([str(a.tape_dump),"--external",str(native)]):
                if r["healthy"]!="1":
                    continue
                t=int(r["receive_wall_ns"]);venue=int(r["venue"]);typ=int(r["event_type"])
                if venue==1 and typ==2:
                    px=float(r["trade_price"])
                    if px>0:
                        binance.append((t,px))
                elif venue==2 and typ==1:
                    bid=float(r["bid"]);ask=float(r["ask"])
                    if bid>0 and ask>=bid:
                        coinbase.append((t,.5*(bid+ask)))
    binance=sorted(set(binance));coinbase=sorted(set(coinbase))
    if not binance or not coinbase:
        raise SystemExit("missing_external_input")
    bt=[x[0] for x in binance];ct=[x[0] for x in coinbase]

    dev=protocol["development_semantics"]
    grid_ns=int(protocol["trigger_protocol"]["grid_ms"])*1_000_000
    warmup_ns=int(dev["overlap_warmup_ms"])*1_000_000
    tail_ns=int(dev["overlap_tail_ms"])*1_000_000
    win_ns=int(rule["shock_window_ms"])*1_000_000
    cooldown_ns=int(rule["trigger_cooldown_ms"])*1_000_000
    threshold=float(rule["minimum_absolute_log_return_bp"])
    start=max(st[0],bt[0],ct[0])+warmup_ns
    end=min(st[-1],bt[-1],ct[-1])-tail_ns
    if end<=start:
        raise SystemExit("no_overlap")

    triggers=[];last=-10**30;t=start
    while t<=end:
        b0=step_le(bt,binance,t-win_ns);b1=step_le(bt,binance,t)
        c0=step_le(ct,coinbase,t-win_ns);c1=step_le(ct,coinbase,t)
        if b0 and b1 and c0 and c1:
            sb=10000.0*math.log(b1[1]/b0[1]);sc=10000.0*math.log(c1[1]/c0[1])
            if abs(sb)+1e-12>=threshold and (sc==0.0 or sb*sc>0.0) and t-last>=cooldown_ns:
                triggers.append((t,sb,sc));last=t
        t+=grid_ns

    def snapshot_at(t):
        return step_le(st,snaps,t)
    def future_mid(snapshot,outcome):
        if snapshot is None:
            return None
        book=snapshot[1] if outcome==1 else snapshot[2]
        return .5*(book[0]+book[1])
    def first_fill(outcome,side,px,queue_ahead,t0,t1,own):
        aggressor=-1 if side=="BUY" else 1;cum=0.0
        for tt,oo,ss,tp,tq in trades:
            if tt<t0:
                continue
            if tt>t1:
                break
            if oo!=outcome or ss!=aggressor:
                continue
            if side=="BUY" and tp>px+1e-12:
                continue
            if side=="SELL" and tp+1e-12<px:
                continue
            cum+=tq
            if cum>queue_ahead+1e-12:
                return {"receive_ns":tt,"quantity":min(own,max(0.0,cum-queue_ahead))}
        return None

    rows=[];invalid_labels=0
    quote_size=float(protocol["incumbent_proxy"]["quote_size_shares"])
    fill_window=int(protocol["incumbent_proxy"]["primary_fill_window_ms"])*1_000_000
    cancel_ns=int(protocol["overlay"]["effective_cancel_latency_ms"])*1_000_000
    stress_q=float(protocol["stress"]["queue_ahead_multiplier"])
    stress_cancel=int(protocol["stress"]["effective_cancel_latency_ms"])*1_000_000
    protocol_sha=sha256_file(a.protocol)
    boundary=int(protocol["promotion_evidence_market_started_strictly_after_ms"])
    eligible=int(manifest["started_ms"])>boundary

    for trig_i,(tt,sb,sc) in enumerate(triggers):
        snap=snapshot_at(tt)
        if snap is None:
            continue
        yes,no=snap[1],snap[2]
        stale=[(1,"SELL",yes),(2,"BUY",no)] if sb>0 else [(1,"BUY",yes),(2,"SELL",no)]
        for outcome,side,book in stale:
            bid,ask,bq,aq=book;px=bid if side=="BUY" else ask
            visible=bq if side=="BUY" else aq
            qa=float(rule["queue_ahead_multiplier"])*visible
            baseline=first_fill(outcome,side,px,qa,tt,tt+fill_window,quote_size)
            overlay=first_fill(outcome,side,px,qa,tt,min(tt+fill_window,tt+cancel_ns),quote_size)
            stress_qa=stress_q*visible
            stress_base=first_fill(outcome,side,px,stress_qa,tt,tt+fill_window,quote_size)
            stress_over=first_fill(outcome,side,px,stress_qa,tt,min(tt+fill_window,tt+stress_cancel),quote_size)
            if overlay and not baseline:
                raise RuntimeError("overlay_created_fill")
            if stress_over and not stress_base:
                raise RuntimeError("stress_overlay_created_fill")
            labels={};label_ok=True
            for h in protocol["labels"]["horizons_ms"]:
                mid=future_mid(snapshot_at(tt+int(h)*1_000_000),outcome)
                if mid is None:
                    if baseline or overlay or (int(h)==500 and (stress_base or stress_over)):
                        label_ok=False
                    continue
                labels[str(h)]=(mid-px) if side=="BUY" else (px-mid)
            if not label_ok:
                invalid_labels+=1;continue
            bmarks={str(h):labels[str(h)] for h in protocol["labels"]["horizons_ms"]} if baseline else {}
            omarks={str(h):labels[str(h)] for h in protocol["labels"]["horizons_ms"]} if overlay else {}
            sbmarks={"500":labels["500"]} if stress_base else {}
            somarks={"500":labels["500"]} if stress_over else {}
            rows.append({
              "schema":EPISODE_SCHEMA,"market_id":str(a.market),
              "quote_id":f"{a.market}-{tt//1_000_000}-{trig_i}-{outcome}-{side}",
              "quote_receive_ms":tt//1_000_000,"maker_model_published_ms":int(protocol["maker_model_published_ms"]),
              "maker_model_sha":protocol["maker_model_sha"],"rule_sha256":RULE_SHA,"book_tape_schema":2,
              "receive_time_causal":not causal_viol,"causality_violations":list(causal_viol),"trigger_applied":True,
              "quote_size_shares":quote_size,"baseline_fill":baseline is not None,"overlay_fill":overlay is not None,
              "baseline_filled_shares":0.0 if not baseline else baseline["quantity"],
              "overlay_filled_shares":0.0 if not overlay else overlay["quantity"],
              "baseline_markout_per_share":bmarks,"overlay_markout_per_share":omarks,
              "stress":{"queue_3x_cancel_200ms":{
                 "baseline_fill":stress_base is not None,"overlay_fill":stress_over is not None,
                 "baseline_filled_shares":0.0 if not stress_base else stress_base["quantity"],
                 "overlay_filled_shares":0.0 if not stress_over else stress_over["quantity"],
                 "baseline_markout_per_share":sbmarks,"overlay_markout_per_share":somarks}},
              "research_provenance":{"protocol_sha256":protocol_sha,"trigger_receive_ns":tt,
                 "binance_return_100ms_bp":sb,"coinbase_return_100ms_bp":sc,"outcome":"YES" if outcome==1 else "NO",
                 "side":side,"quote_price":px,"queue_ahead_shares":qa,
                 "baseline_fill_receive_ms":None if not baseline else baseline["receive_ns"]//1_000_000,
                 "overlay_fill_receive_ms":None if not overlay else overlay["receive_ns"]//1_000_000,
                 "stress_baseline_fill_receive_ms":None if not stress_base else stress_base["receive_ns"]//1_000_000,
                 "market_started_ms":int(manifest["started_ms"]),"promotion_eligible_market":eligible}
            })

    exclusion=[]
    if invalid_labels:
        exclusion.append("MISSING_REQUIRED_FILL_LABELS")
    if causal_viol:
        exclusion.append("BOOK_CAUSALITY_VIOLATION")
    market_evaluable=not exclusion
    emitted=rows if market_evaluable else []
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open("w") as f:
        for row in emitted:
            f.write(json.dumps(row,sort_keys=True,separators=(",",":"))+"\n")
    avoidable=[r for r in emitted if r["baseline_fill"] and not r["overlay_fill"]]
    stress_avoidable=[r for r in emitted if r["stress"]["queue_3x_cancel_200ms"]["baseline_fill"] and not r["stress"]["queue_3x_cancel_200ms"]["overlay_fill"]]
    summary={
      "schema":"polymarket_v7_btc_m5_external_cancel_episode_build_summary_v2","market_id":str(a.market),
      "manifest":str(mp),"market_started_ms":int(manifest["started_ms"]),"promotion_boundary_ms":boundary,
      "promotion_eligible_market":eligible,"market_evaluable":market_evaluable,"evidence_valid":market_evaluable,
      "exclusion_reason_codes":exclusion,"protocol_sha256":protocol_sha,"rule_sha256":RULE_SHA,
      "triggers":len(triggers),"episode_attempts":len(rows)+invalid_labels,"episodes":len(emitted),
      "baseline_fills":sum(r["baseline_fill"] for r in emitted),"overlay_fills":sum(r["overlay_fill"] for r in emitted),
      "avoidable_fills":len(avoidable),"avoidable_filled_shares":sum(r["baseline_filled_shares"] for r in avoidable),
      "stress_avoidable_fills":len(stress_avoidable),
      "stress_avoidable_filled_shares":sum(r["stress"]["queue_3x_cancel_200ms"]["baseline_filled_shares"] for r in stress_avoidable),
      "invalid_label_episodes":invalid_labels,"causality_violations":causal_viol,
      "binance_events":len(binance),"coinbase_events":len(coinbase),"book_events":book_events,"trades":len(trades),
      "output_sha256":sha256_file(a.output),
      "source_provenance":{"manifest_sha256":sha256_file(mp),
        "book_segments":[{"path":str(p),"sha256":sha256_file(p)} for p in segs],
        "external_segments":[{"path":str(p),"sha256":sha256_file(p)} for p in a.external_tape],
        "tape_dump_sha256":sha256_file(a.tape_dump)},
    }
    a.summary.write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n")
    print(json.dumps(summary,sort_keys=True))


if __name__=="__main__":
    main()
