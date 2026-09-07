#!/usr/bin/env python3
import argparse,bisect,csv,gzip,glob,hashlib,json,math,shutil,subprocess,tempfile
from contextlib import contextmanager
from pathlib import Path

def run_csv(cmd):
    p=subprocess.Popen(cmd,stdout=subprocess.PIPE,text=True,bufsize=1)
    try:
        for row in csv.DictReader(p.stdout): yield row
    finally:
        if p.stdout: p.stdout.close()
        rc=p.wait()
        if rc: raise RuntimeError(f"decoder_failed:{cmd[0]}:{rc}")

@contextmanager
def materialized(path: Path):
    if path.suffix != '.gz':
        yield path
        return
    with tempfile.NamedTemporaryFile(suffix='.bin') as tmp:
        with gzip.open(path,'rb') as src:
            shutil.copyfileobj(src,tmp,length=4*1024*1024)
        tmp.flush()
        yield Path(tmp.name)

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''): h.update(block)
    return h.hexdigest()

def step_le(ts,vals,t,max_age_ns):
    i=bisect.bisect_right(ts,t)-1
    if i<0: return None
    v=vals[i]
    return v if t-v[0] <= max_age_ns else None

def first_ge(ts,vals,t,max_lag_ns):
    i=bisect.bisect_left(ts,t)
    if i>=len(vals): return None
    v=vals[i]
    return v if v[0]-t <= max_lag_ns else None

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--market',required=True)
    ap.add_argument('--external-tape',type=Path,nargs='+',required=True)
    ap.add_argument('--tape-dump',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--summary',type=Path,required=True)
    a=ap.parse_args()
    protocol=json.load(open(a.root/'episode_protocol.json'))
    if protocol['canonical_rule_sha256'] != '9e8c7e6a1d7e4a87cd9977396bcbbb228f96b4e35e4a34e84e1514e9e9630254':
        raise SystemExit('unexpected_rule_hash')
    rule=protocol['canonical_rule']
    if (rule['shock_source'],rule['shock_window_ms'],rule['minimum_absolute_log_return_bp'],rule['confirmation_source'],rule['trigger_cooldown_ms']) != ('BINANCE_SPOT_TRADES',100,0.3,'COINBASE_SPOT_TOP_OF_BOOK',250):
        raise SystemExit('rule_drift')
    manifests=[]
    for p in a.root.glob(f'btc-m5-book.{a.market}.*.manifest.json'):
        d=json.load(open(p)); manifests.append((p,d))
    if len(manifests)!=1: raise SystemExit(f'manifest_count:{len(manifests)}')
    mp,manifest=manifests[0]
    if int(manifest.get('payload_schema_version',0))!=2: raise SystemExit('book_schema')
    segs=sorted((a.root/'normalized_events').glob(f'btc-m5-book.{a.market}.*.bin'))
    if not segs: raise SystemExit('no_closed_book_segments')
    # Load per-outcome book states and public trades, preserving receive-time order.
    books={1:[],2:[]}; trades=[]; seq_prev=0; causal_viol=[]
    for r in run_csv([str(a.tape_dump),'--book',*[str(x) for x in segs]]):
        seq=int(r['seq']); wall_ns=int(r['receive_ms'])*1_000_000; outcome=int(r['outcome']); kind=int(r['kind'])
        if seq<=seq_prev: causal_viol.append(f'book_sequence_nonmonotone:{seq_prev}->{seq}')
        seq_prev=seq
        if kind==1:
            bid=float(r['bid']);ask=float(r['ask']);bq=float(r['bidq']);aq=float(r['askq'])
            if outcome in (1,2) and 0<bid<ask<1 and bq>=0 and aq>=0:
                books[outcome].append((wall_ns,bid,ask,bq,aq))
        elif kind==2:
            px=float(r['trade_price']);qty=float(r['trade_qty']);side=int(r['trade_side'])
            if outcome in (1,2) and px>0 and qty>0 and side in (-1,1):
                trades.append((wall_ns,outcome,side,px,qty))
    for o in (1,2): books[o].sort(key=lambda x:x[0])
    trades.sort(key=lambda x:x[0])
    if not books[1] or not books[2]: raise SystemExit('missing_outcome_book')
    # Load only the two frozen external inputs from normalized venue evidence.
    binance=[];coinbase=[]
    for tape in a.external_tape:
        with materialized(tape) as native:
            for r in run_csv([str(a.tape_dump),'--external',str(native)]):
                if r['healthy']!='1': continue
                t=int(r['receive_wall_ns']);venue=int(r['venue']);typ=int(r['event_type'])
                if venue==1 and typ==2:
                    px=float(r['trade_price'])
                    if px>0: binance.append((t,px))
                elif venue==2 and typ==1:
                    bid=float(r['bid']);ask=float(r['ask'])
                    if bid>0 and ask>=bid: coinbase.append((t,.5*(bid+ask)))
    # Exact duplicates can arise only if caller supplied duplicate tapes; remove them deterministically.
    binance=sorted(set(binance));coinbase=sorted(set(coinbase))
    bt=[x[0] for x in binance];ct=[x[0] for x in coinbase]
    if not binance or not coinbase: raise SystemExit('missing_external_input')
    max_age=int(protocol['inputs']['source_max_age_ms'])*1_000_000
    max_label_lag=int(protocol['inputs']['future_label_max_lag_ms'])*1_000_000
    grid_ns=int(protocol['trigger_protocol']['grid_ms'])*1_000_000
    win_ns=int(rule['shock_window_ms'])*1_000_000
    cooldown_ns=int(rule['trigger_cooldown_ms'])*1_000_000
    threshold=float(rule['minimum_absolute_log_return_bp'])
    # Require enough future book for the longest frozen label. No imputation at segment edges.
    start=max(books[1][0][0],books[2][0][0],bt[0],ct[0])+max_age
    end=min(books[1][-1][0],books[2][-1][0],bt[-1],ct[-1])-1_100_000_000
    if end<=start: raise SystemExit('no_overlap')
    # Align grid to absolute receive-wall 25ms buckets, matching development protocol.
    t=((start+grid_ns-1)//grid_ns)*grid_ns
    triggers=[]; last=-10**30
    while t<=end:
        b0=step_le(bt,binance,t-win_ns,max_age);b1=step_le(bt,binance,t,max_age)
        c0=step_le(ct,coinbase,t-win_ns,max_age);c1=step_le(ct,coinbase,t,max_age)
        if b0 and b1 and c0 and c1:
            sb=10000.0*math.log(b1[1]/b0[1]); sc=10000.0*math.log(c1[1]/c0[1])
            if abs(sb)+1e-12>=threshold and sb*sc>=0.0 and t-last>=cooldown_ns:
                triggers.append((t,sb,sc));last=t
        t+=grid_ns
    bts={o:[x[0] for x in books[o]] for o in (1,2)}
    def book_at(outcome,t): return step_le(bts[outcome],books[outcome],t,max_age)
    def future_mid(outcome,t):
        x=first_ge(bts[outcome],books[outcome],t,max_label_lag)
        return None if x is None else .5*(x[1]+x[2])
    def fill(outcome,side,px,qa,t0,t1,own=5.0):
        # side BUY is hit by seller-initiated (-1); side SELL lifted by buyer-initiated (+1).
        aggr=-1 if side=='BUY' else 1;cum=0.0
        for tt,oo,ss,tp,tq in trades:
            if tt<t0: continue
            if tt>t1: break
            if oo!=outcome or ss!=aggr: continue
            if side=='BUY' and tp>px+1e-12: continue
            if side=='SELL' and tp+1e-12<px: continue
            cum+=tq
            if cum>qa+1e-12:
                return {'receive_ns':tt,'quantity':min(own,max(0.0,cum-qa))}
        return None
    rows=[]; invalid_labels=0; quote_size=float(protocol['incumbent_proxy']['quote_size_shares'])
    fill_window=int(protocol['incumbent_proxy']['primary_fill_window_ms'])*1_000_000
    cancel_ns=int(protocol['overlay']['effective_cancel_latency_ms'])*1_000_000
    stress_q=float(protocol['stress']['queue_ahead_multiplier']);stress_cancel=int(protocol['stress']['effective_cancel_latency_ms'])*1_000_000
    for trig_i,(tt,sb,sc) in enumerate(triggers):
        stale=[(1,'SELL'),(2,'BUY')] if sb>0 else [(1,'BUY'),(2,'SELL')]
        for outcome,side in stale:
            b=book_at(outcome,tt)
            if b is None: continue
            _,bid,ask,bq,aq=b
            px=bid if side=='BUY' else ask
            qa=float(protocol['incumbent_proxy']['queue_ahead_multiplier'])*(bq if side=='BUY' else aq)
            baseline=fill(outcome,side,px,qa,tt,tt+fill_window)
            overlay=fill(outcome,side,px,qa,tt,min(tt+fill_window,tt+cancel_ns))
            stress_qa=stress_q*(bq if side=='BUY' else aq)
            stress_base=fill(outcome,side,px,stress_qa,tt,tt+fill_window)
            stress_over=fill(outcome,side,px,stress_qa,tt,min(tt+fill_window,tt+stress_cancel))
            if overlay and not baseline: raise RuntimeError('overlay_created_fill')
            if stress_over and not stress_base: raise RuntimeError('stress_overlay_created_fill')
            labels={}
            label_ok=True
            for h in protocol['labels']['horizons_ms']:
                mid=future_mid(outcome,tt+int(h)*1_000_000)
                if mid is None:
                    if baseline or overlay or (int(h)==500 and (stress_base or stress_over)): label_ok=False
                    continue
                labels[str(h)]=(mid-px) if side=='BUY' else (px-mid)
            if not label_ok:
                invalid_labels+=1; continue
            baseline_marks={str(h):labels[str(h)] for h in protocol['labels']['horizons_ms']} if baseline else {}
            overlay_marks={str(h):labels[str(h)] for h in protocol['labels']['horizons_ms']} if overlay else {}
            stress_base_marks={'500':labels['500']} if stress_base else {}
            stress_over_marks={'500':labels['500']} if stress_over else {}
            rows.append({
              'schema':'polymarket_v7_btc_m5_external_cancel_forward_episode_v1',
              'market_id':str(a.market),'quote_id':f"{a.market}-{tt//1_000_000}-{trig_i}-{outcome}-{side}",
              'quote_receive_ms':tt//1_000_000,
              'maker_model_published_ms':int(protocol['maker_model_published_ms']),
              'maker_model_sha':protocol['maker_model_sha'],
              'rule_sha256':protocol['canonical_rule_sha256'],'book_tape_schema':2,
              'receive_time_causal':not causal_viol,'causality_violations':list(causal_viol),
              'trigger_applied':True,'quote_size_shares':quote_size,
              'baseline_fill':baseline is not None,'overlay_fill':overlay is not None,
              'baseline_markout_per_share':baseline_marks,'overlay_markout_per_share':overlay_marks,
              'stress':{'queue_3x_cancel_200ms':{
                  'baseline_fill':stress_base is not None,'overlay_fill':stress_over is not None,
                  'baseline_markout_per_share':stress_base_marks,'overlay_markout_per_share':stress_over_marks}},
              'research_provenance':{
                 'protocol_sha256':hashlib.sha256((a.root/'episode_protocol.json').read_bytes()).hexdigest(),
                 'trigger_receive_ns':tt,'binance_return_100ms_bp':sb,'coinbase_return_100ms_bp':sc,
                 'outcome':'YES' if outcome==1 else 'NO','side':side,'quote_price':px,'queue_ahead_shares':qa,
                 'baseline_fill_receive_ms':None if not baseline else baseline['receive_ns']//1_000_000,
                 'baseline_fill_quantity_shares':0.0 if not baseline else baseline['quantity'],
                 'overlay_fill_receive_ms':None if not overlay else overlay['receive_ns']//1_000_000,
                 'stress_baseline_fill_receive_ms':None if not stress_base else stress_base['receive_ns']//1_000_000,
                 'market_started_ms':int(manifest['started_ms']),
                 'promotion_eligible_market':int(manifest['started_ms'])>int(protocol['promotion_evidence_market_started_strictly_after_ms'])
              }
            })
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('w') as f:
        for row in rows: f.write(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n')
    summary={
      'schema':'polymarket_v7_btc_m5_external_cancel_episode_build_summary_v1','market_id':str(a.market),
      'manifest':str(mp),'market_started_ms':int(manifest['started_ms']),
      'promotion_boundary_ms':int(protocol['promotion_evidence_market_started_strictly_after_ms']),
      'promotion_eligible_market':int(manifest['started_ms'])>int(protocol['promotion_evidence_market_started_strictly_after_ms']),
      'protocol_sha256':hashlib.sha256((a.root/'episode_protocol.json').read_bytes()).hexdigest(),
      'rule_sha256':protocol['canonical_rule_sha256'],'triggers':len(triggers),'episodes':len(rows),
      'baseline_fills':sum(r['baseline_fill'] for r in rows),'overlay_fills':sum(r['overlay_fill'] for r in rows),
      'avoidable_fills':sum(r['baseline_fill'] and not r['overlay_fill'] for r in rows),
      'stress_baseline_fills':sum(r['stress']['queue_3x_cancel_200ms']['baseline_fill'] for r in rows),
      'stress_overlay_fills':sum(r['stress']['queue_3x_cancel_200ms']['overlay_fill'] for r in rows),
      'invalid_label_episodes_skipped':invalid_labels,'causality_violations':causal_viol,
      'binance_events':len(binance),'coinbase_events':len(coinbase),'book_events':sum(len(v) for v in books.values()),'trades':len(trades),
      'output_sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),
      'source_provenance':{
        'manifest_sha256':sha256_file(mp),
        'book_segments':[{'path':str(p),'sha256':sha256_file(p)} for p in segs],
        'external_segments':[{'path':str(p),'sha256':sha256_file(p)} for p in a.external_tape],
        'tape_dump_sha256':sha256_file(a.tape_dump),
      }
    }
    a.summary.write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n')
    print(json.dumps(summary,sort_keys=True))
if __name__=='__main__': main()
