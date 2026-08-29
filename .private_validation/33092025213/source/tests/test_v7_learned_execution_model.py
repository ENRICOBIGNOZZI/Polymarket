#!/usr/bin/env python3
from __future__ import annotations
import math, sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]; SCRIPTS=ROOT/'scripts'; sys.path.insert(0,str(SCRIPTS))
from v7_learned_execution_model import (
    FEATURES, ExecutionModelError, JointExample, Kernel, analyze, build_joint, build_orders,
    joint_feature_names, joint_report, predict_distribution, product_marginal_probability, split,
)
SHA='a'*40

def ev(kind, oid='', **kw):
    d=dict(event_type=kind,strategy='GRAPH_RV',model_sha=SHA,paper_only=True,authenticated_execution=False,
        order_id=oid or None,candidate_id=None,opportunity_id=None,bundle_id=None,fill_id=None,leg_id=None,
        token_id=None,side=None,book_snapshot_id=None,recorded_ts_ms=10_000,receive_ts_ms=None,decision_ts_ms=None,
        exchange_ts_ms=None,bid=None,ask=None,bid_depth=None,ask_depth=None,queue_ahead=None,intended_size=None,
        intended_action=None,predicted_alpha=None,expected_ev=None,timeout_ms=None,filled_size=None,fill_price=None,
        limit_price=None,fee=None,fee_source=None,executable_liquidation_value=None,complete=None,order_state=None,
        markouts={},metadata={}); d.update(kw); return SimpleNamespace(**d)

def sub(oid,ts,q,*,bundle=None,leg=None,strategy='GRAPH_RV',ids=None,count=2,imb=0.0):
    bd=100*(1+imb); md={}
    if bundle and count is not None:
        md['expected_leg_count']=count
        if ids is not None: md['expected_leg_ids']=list(ids)
    return ev('ORDER_SUBMITTED',oid,strategy=strategy,bundle_id=bundle,leg_id=leg,token_id=f't-{oid}',side='BUY',
        book_snapshot_id=f'b-{oid}',recorded_ts_ms=ts+3,exchange_ts_ms=ts-1,receive_ts_ms=ts,decision_ts_ms=ts+2,
        bid=.49,ask=.51,bid_depth=bd,ask_depth=200-bd,queue_ahead=q,intended_size=10.,limit_price=.49,
        intended_action='JOIN_MAKER',predicted_alpha=.01,expected_ev=.002,timeout_ms=60_000,metadata=md)

def fill(oid,ts,qty=10.,*,fid=None,bundle=None,leg=None,strategy='GRAPH_RV',token=None,complete=None):
    if complete is None: complete=qty>=10
    return ev('FILL',oid,strategy=strategy,bundle_id=bundle,leg_id=leg,fill_id=fid or f'f-{oid}',token_id=token or f't-{oid}',side='BUY',
        recorded_ts_ms=ts,exchange_ts_ms=ts-2,receive_ts_ms=ts-1,fill_price=.5,filled_size=qty,fee=.001,
        fee_source='test_authoritative',complete=complete)

def timeout(oid,ts,strategy='GRAPH_RV'): return ev('ORDER_STATE',oid,strategy=strategy,recorded_ts_ms=ts,order_state='TIMEOUT')

def mark(oid,fill_ts,val,h='60s',*,fid=None,bundle=None,leg=None,strategy='GRAPH_RV',token=None,delta=100):
    ms={'1s':1000,'10s':10000,'45s':45000,'60s':60000,'300s':300000}[h]; ts=fill_ts+ms+delta
    return ev('MARKOUT',oid,strategy=strategy,bundle_id=bundle,leg_id=leg,fill_id=fid or f'f-{oid}',token_id=token or f't-{oid}',side='BUY',
        recorded_ts_ms=ts,exchange_ts_ms=ts-2,receive_ts_ms=ts-1,book_snapshot_id=f'm-{oid}-{h}',
        executable_liquidation_value=.49,markouts={h:val})

def raises(rows,text):
    try: build_orders(rows,SHA)
    except ExecutionModelError as e: assert text in str(e); return
    raise AssertionError(f'expected {text}')

def test_safety_missingness_and_censoring():
    x=sub('x',1000,5); x.model_sha='b'*40; raises([x],'mixed_sha')
    x=sub('x',1000,5); x.authenticated_execution=True; raises([x],'authenticated_execution_forbidden')
    bad=sub('bad',1000,None); good=sub('good',2000,5); rows,st=build_orders([bad,timeout('bad',1100),good,timeout('good',2100),sub('open',3000,5)],SHA)
    assert [r.order_id for r in rows]==['good'] and st['excluded_missing_or_invalid_features']==1 and st['unresolved_orders']==1

def test_clock_lineage_fill_integrity():
    bad=sub('c',1000,5); bad.exchange_ts_ms=1005; rows,st=build_orders([bad,timeout('c',1100)],SHA); assert not rows and st['excluded_missing_or_invalid_features']==1
    raises([sub('t',2000,5),fill('t',2100,token='wrong')],'fill:lineage_mismatch')
    raises([sub('o',3000,5),fill('o',3100,6,fid='1',complete=False),fill('o',3200,6,fid='2',complete=False)],'overfill')
    raises([sub('p',4000,5),fill('p',4100,4,complete=True)],'complete_below_intended')

def test_markout_maturity_lineage_and_full_fill_coverage():
    fts=5100; raises([sub('m',5000,5),fill('m',fts),mark('m',fts,.01,token='wrong')],'markout:lineage_mismatch')
    prem=mark('q',6100,.01,'300s'); prem.recorded_ts_ms=7100; prem.exchange_ts_ms=7098; prem.receive_ts_ms=7099
    raises([sub('q',6000,5),fill('q',6100),prem],'markout:not_mature')
    rows=[sub('z',8000,5),fill('z',8100,5,fid='1',complete=False),fill('z',8200,5,fid='2',complete=False),mark('z',8100,.01,fid='1')]
    o,st=build_orders(rows,SHA); assert '60s' not in o[0].markouts and st['incomplete_markout_coverage_60s']==1
    rows.append(mark('z',8200,.03,fid='2')); o,_=build_orders(rows,SHA); assert abs(o[0].markouts['60s']-.02)<1e-12

def test_label_maturity_embargo():
    rows=[SimpleNamespace(ts_ms=i*1000,label_ts_ms=i*1000+(10000 if i in {12,13,14} else 100),order_id=str(i)) for i in range(20)]
    tr,te=split(rows,8,4,.25,2000); assert te[0].ts_ms==15000 and all(r.label_ts_ms<=13000 for r in tr)

def test_fill_and_markout_models_oos():
    events=[]
    for i in range(240):
        q=float(i%100); oid=f'o{i}'; ts=1000+i*100
        events += [sub(oid,ts,q), fill(oid,ts+20) if q<40 else timeout(oid,ts+20)]
    r=analyze(events,SHA,min_order_train=120,min_order_test=40,min_markout_train=999,min_markout_test=999,min_joint_train=999,min_joint_test=999,bandwidth=.5)
    fm=r['strategy_models']['GRAPH_RV']['fill_model']; assert fm['state']=='OOS_SCORED' and fm['oos_brier']<.35*fm['baseline_brier']
    events=[]
    for i in range(320):
        im=-.9+1.8*(i%80)/79; oid=f'm{i}'; ts=1000+i*100000; events.append(sub(oid,ts,10 if i%4!=3 else 90,imb=im))
        if i%4!=3: fts=ts+20; events += [fill(oid,fts),mark(oid,fts,.02*im-.003)]
        else: events.append(timeout(oid,ts+20))
    r=analyze(events,SHA,min_order_train=160,min_order_test=60,min_markout_train=80,min_markout_test=20,min_joint_train=999,min_joint_test=999,bandwidth=.5)
    mm=r['strategy_models']['GRAPH_RV']['markout_models']['60s']; assert mm['state']=='OOS_SCORED' and mm['oos_rmse']<mm['baseline_rmse']

def test_bundle_completeness_and_ordered_asymmetry():
    rows=[sub('a',1000,5,bundle='ok',leg='1',ids=['2','1']),timeout('a',1100),sub('b',1010,90,bundle='ok',leg='2',ids=['2','1']),timeout('b',1110),
          sub('c',2000,5,bundle='inc',leg='1',ids=['1','2']),timeout('c',2100),sub('d',2010,5,bundle='inc',leg='2',ids=['1','2'])]
    o,_=build_orders(rows,SHA); j,st=build_joint(o); assert len(j)==1 and st['skipped_incomplete_bundle']==1
    assert abs(j[0].x[0]-math.log1p(90))<1e-12 and abs(j[0].x[len(FEATURES)]-math.log1p(5))<1e-12
    names=joint_feature_names(2,['2','1']); assert names[0].startswith('leg[2]_') and names[len(FEATURES)].startswith('leg[1]_')

def test_partial_joint_state_and_no_candidate_coalescing():
    rows=[sub('a',1000,5,bundle='b',leg='1',ids=['1','2']),fill('a',1100,bundle='b',leg='1'),
          sub('b',1001,5,bundle='b',leg='2',ids=['1','2']),fill('b',1101,4,bundle='b',leg='2',complete=False),timeout('b',1200),
          sub('x',2000,5,leg='1'),timeout('x',2100),sub('y',2001,5,leg='2'),timeout('y',2101)]
    o,_=build_orders(rows,SHA); j,st=build_joint(o); assert len(j)==1 and j[0].state=='COMPLETE|PARTIAL' and st['skipped_missing_bundle_id']==2

def test_joint_distribution_not_marginal_product_and_stratified():
    base=[JointExample(str(i),'GRAPH_RV',('1','2'),2,i,i,(0.,)*4,'COMPLETE|COMPLETE' if i%2==0 else 'NO_FILL|NO_FILL') for i in range(200)]
    k=Kernel.fit([r.x for r in base],1.); lab=[r.state for r in base]; d=predict_distribution(k,base[0].x,lab)
    direct=-.5*(math.log(d['COMPLETE|COMPLETE'])+math.log(d['NO_FILL|NO_FILL']))
    marginal=-.5*(math.log(product_marginal_probability(lab,'COMPLETE|COMPLETE'))+math.log(product_marginal_probability(lab,'NO_FILL|NO_FILL'))); assert direct+.5<marginal
    rows=[]
    for strategy,sig,off in [('GRAPH_RV',('buy','sell'),0),('GRAPH_RV',('left','right'),10000),('HARD_ARB',('buy','sell'),20000)]:
        for i in range(80): rows.append(JointExample(f'{strategy}-{sig}-{i}',strategy,sig,2,off+i*100,off+i*100+10,(float(i%5),)*4,'COMPLETE|COMPLETE' if i%2==0 else 'NO_FILL|NO_FILL'))
    r=joint_report(rows,1.,.25,0,40,15); assert set(r)=={'GRAPH_RV::buy|sell','GRAPH_RV::left|right','HARD_ARB::buy|sell'}

def test_strategy_stratification_and_fail_closed_report_contract():
    events=[]
    for strategy,rev,base in [('GRAPH_RV',False,1000),('MICRO_MAKER',True,100000)]:
        for i in range(120):
            q=float(i%60); oid=f'{strategy}-{i}'; ts=base+i*100; s=sub(oid,ts,q,strategy=strategy); f=fill(oid,ts+20,strategy=strategy) if (q>=30 if rev else q<30) else timeout(oid,ts+20,strategy)
            events += [s,f]
    r=analyze(events,SHA,min_order_train=60,min_order_test=20,min_markout_train=999,min_markout_test=999,min_joint_train=999,min_joint_test=999,bandwidth=.5)
    assert set(r['strategy_models'])=={'GRAPH_RV','MICRO_MAKER'} and 'fill_model' not in r and r['promotion_allowed'] is False and r['decision']=='MORE_EVIDENCE_REQUIRED'
    c=r['causal_contract']; assert c['markout_horizon_maturity']=='exchange_and_receive_clock_enforced' and c['markout_fill_coverage']=='all_fills_required_per_order_horizon'
    assert c['order_model_pooling']=='strategy_stratified_only' and c['joint_pooling']=='strategy_and_leg_signature_stratified' and c['product_of_marginals_role']=='benchmark_only'

if __name__=='__main__':
    tests=[fn for name,fn in sorted(globals().items()) if name.startswith('test_') and callable(fn)]
    for t in tests: t()
    print(f'ok {len(tests)} v7 learned execution tests')
