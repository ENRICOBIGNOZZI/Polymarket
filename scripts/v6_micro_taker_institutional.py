#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, math, os, statistics, time, urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any
import v6_micro_taker as base
from v6_micro_target import label_matured_samples
from v6_execution_model import clamp, fee_per_share, mean_variance_notional, robust_edge_lcb, state_slippage_bps, taker_cost, walk_levels


def finite(v: Any, d: float=math.nan)->float:
    try: x=float(v)
    except (TypeError,ValueError,OverflowError): return d
    return x if math.isfinite(x) else d


def hist_features(rows:list[list[float]],now:int,mid:float)->tuple[float,float,float]:
    clean=sorted((int(finite(x[0],0)),finite(x[1])) for x in rows if isinstance(x,list) and len(x)>=2)
    clean=[x for x in clean if x[0]>0 and math.isfinite(x[1]) and now-x[0]<=180]
    prices=[p for _,p in clean]+[mid]; rets=[prices[i]-prices[i-1] for i in range(1,len(prices))]
    vol=statistics.pstdev(rets) if len(rets)>=3 else 0.0
    def past(age:int)->float:
        return next((p for ts,p in reversed(clean) if now-ts>=age),mid)
    return vol,mid-past(25),mid-past(80)


def rich_features(m:base.Market,y:base.Book,n:base.Book,hist:list[list[float]],now:int):
    mid=y.mid(); spread=max(y.spread(),n.spread())
    if not math.isfinite(mid) or not math.isfinite(spread) or spread<=0:return None
    ym,nm=y.micro(),n.micro()
    if not math.isfinite(ym) or not math.isfinite(nm):return None
    def raw(b:base.Book,bid:bool,k:int)->float:return sum(q for _,q in (b.bids if bid else b.asks)[:k])
    yb1,ya1,yb5,ya5=raw(y,True,1),raw(y,False,1),raw(y,True,5),raw(y,False,5)
    nb5,na5=raw(n,True,5),raw(n,False,5)
    vol,m30,m90=hist_features(hist,now,mid); scale=max(spread,1e-4)
    liq=clamp(math.log1p(m.liq)/math.log1p(100000),0,1)
    x=[1.0,clamp((ym-mid)/spread,-3,3),clamp(((1-nm)-mid)/spread,-3,3),
       clamp((yb1-ya1)/(yb1+ya1+1e-9),-1,1),clamp((yb5-ya5)/(yb5+ya5+1e-9),-1,1),
       clamp((na5-nb5)/(na5+nb5+1e-9),-1,1),clamp((ym-(1-nm))/spread,-3,3),
       clamp((math.log1p(yb1+ya1)-math.log1p(max(0,yb5+ya5-yb1-ya1)))/4,-2,2),
       clamp(m30/scale,-3,3),clamp(m90/scale,-3,3),clamp(vol/scale,0,3),
       clamp(spread/max(.01,mid*(1-mid)),0,5),liq]
    return x,mid,spread,vol,liq


def solve(A:list[list[float]],b:list[float])->list[float]:
    n=len(b); M=[A[i][:]+[b[i]] for i in range(n)]
    for i in range(n):
        p=max(range(i,n),key=lambda r:abs(M[r][i]))
        if abs(M[p][i])<1e-12:return [0.0]*n
        M[i],M[p]=M[p],M[i]; d=M[i][i]; M[i]=[z/d for z in M[i]]
        for r in range(n):
            if r==i:continue
            q=M[r][i]
            if abs(q)>1e-15:M[r]=[M[r][c]-q*M[i][c] for c in range(n+1)]
    return [M[i][n] for i in range(n)]


def weighted_ridge(rows:list[dict[str,Any]],p:int,ridge:float=.03,half_life:float=2500)->tuple[list[float],float,int]:
    rows=[r for r in rows if r.get('y') is not None and isinstance(r.get('x'),list) and len(r['x'])==p][-15000:]
    if len(rows)<max(80,4*p):return [0.0]*p,math.inf,len(rows)
    def fit(extra:list[float]|None=None):
        A=[[0.0]*p for _ in range(p)]; b=[0.0]*p; N=len(rows)
        for k,r in enumerate(rows):
            w=math.exp(-math.log(2)*(N-1-k)/max(1,half_life))*(extra[k] if extra else 1)
            x=[finite(z,0) for z in r['x']]; yy=finite(r['y'],0)
            for i in range(p):
                b[i]+=w*x[i]*yy
                for j in range(p):A[i][j]+=w*x[i]*x[j]
        for i in range(1,p):A[i][i]+=ridge
        A[0][0]+=.05*ridge; return solve(A,b)
    beta=fit(); res=[finite(r['y'],0)-sum(beta[i]*finite(r['x'][i],0) for i in range(p)) for r in rows]
    med=statistics.median(res); mad=statistics.median(abs(z-med) for z in res); sc=max(1e-6,1.4826*mad)
    beta=fit([min(1,1.5*sc/max(abs(z-med),1e-12)) for z in res])
    res=[finite(r['y'],0)-sum(beta[i]*finite(r['x'][i],0) for i in range(p)) for r in rows]
    med=statistics.median(res); sigma=max(1e-5,1.4826*statistics.median(abs(z-med) for z in res))
    return beta,sigma,len(rows)


def fee(clob:str,m:base.Market,cache:dict[str,list[float]])->tuple[float,float]:
    if m.yes in cache:return finite(cache[m.yes][0],.07),finite(cache[m.yes][1],1)
    rate,exp=m.fee_rate,m.fee_exp
    try:
        raw=base.request_json(clob+'/fee-rate?token_id='+urllib.parse.quote(m.yes))
        if isinstance(raw,dict) and raw.get('base_fee') is not None:rate=max(0,finite(raw['base_fee'],700)/10000)
    except Exception:pass
    cache[m.yes]=[rate,exp]; return rate,exp


def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--config',type=Path,required=True);ap.add_argument('--run-dir',type=Path,required=True)
    ap.add_argument('--markets',type=int,default=300);ap.add_argument('--min-liquidity',type=float,default=25);ap.add_argument('--horizon-seconds',type=int,default=30)
    ap.add_argument('--max-target-staleness-seconds',type=int,default=10);ap.add_argument('--max-trade-usd',type=float,default=20);ap.add_argument('--min-edge',type=float,default=.0003)
    ap.add_argument('--base-slippage-bps',type=float,default=2);ap.add_argument('--uncertainty-z',type=float,default=.5);ap.add_argument('--risk-budget-fraction',type=float,default=.003)
    ap.add_argument('--max-positions',type=int,default=25);ap.add_argument('--sample-half-life',type=float,default=2500);a=ap.parse_args()
    cfg=json.loads(a.config.read_text()); gamma,clob=cfg['gamma_url'],cfg['clob_url']; start=float(cfg['starting_capital']); now=int(time.time()); a.run_dir.mkdir(parents=True,exist_ok=True)
    sp=a.run_dir/'state.json'; st=json.loads(sp.read_text()) if sp.exists() else {}; cash=finite(st.get('cash'),start);peak=max(start,finite(st.get('peak'),start));killed=bool(st.get('killed',False))
    pos=st.get('positions') if isinstance(st.get('positions'),dict) else {}; samples=st.get('samples') if isinstance(st.get('samples'),list) else []; hist=st.get('market_history') if isinstance(st.get('market_history'),dict) else {}
    realized_total=finite(st.get('realized_pnl_total'),0);fc=st.get('fee_cache') if isinstance(st.get('fee_cache'),dict) else {}; fail=[];rej=Counter()
    try:markets=base.discover(gamma,a.markets,a.min_liquidity);books=base.fetch_books(clob,markets)
    except Exception as e:markets=[];books={};fail.append(f'market_data:{type(e).__name__}:{e}')
    cur={}
    for m in markets:
        y,n=books.get(m.yes),books.get(m.no)
        if not y or not n:rej['missing_book']+=1;continue
        z=rich_features(m,y,n,hist.get(m.id,[]),now)
        if z:cur[m.id]=(m,y,n,z)
    lab=label_matured_samples(samples,now=now,horizon_seconds=a.horizon_seconds,max_target_staleness_seconds=a.max_target_staleness_seconds)
    p=13; beta,sigma,nlab=weighted_ridge(samples,p,half_life=a.sample_half_life); realized=0.0
    for mid,x in list(pos.items()):
        if mid not in cur:continue
        m,y,n,z=cur[mid];side=x['side'];b=y if side=='YES' else n;sh=finite(x['shares'],0);pred=clamp(sum(beta[i]*z[0][i] for i in range(p)),-2.5*z[2],2.5*z[2]);fy=clamp(z[1]+pred,.001,.999)
        if now-int(x['entry_ts'])<a.horizon_seconds and not ((side=='YES' and fy<=z[1]) or (side=='NO' and fy>=z[1])):continue
        w=walk_levels(b.bids,sh,buy=False)
        if not w.depth_complete:rej['exit_depth']+=1;continue
        slip=state_slippage_bps(base_bps=a.base_slippage_bps,spread=b.spread(),short_vol=z[3],participation=sh/max(1e-9,b.bids[0][1]),liquidity_score=z[4]);px=w.vwap*(1-slip/10000);r,e=fee(clob,m,fc);ff=fee_per_share(px,r,e)*sh;pro=px*sh-ff;pnl=pro-finite(x['cost'],0);cash+=pro;realized+=pnl;del pos[mid]
        base.append_csv(a.run_dir/'fills.csv',['timestamp','market_id','slug','action','side','shares','price','fee','pnl','edge_lcb','prediction_sigma'],{'timestamp':now,'market_id':mid,'slug':m.slug,'action':'SELL','side':side,'shares':sh,'price':px,'fee':ff,'pnl':pnl,'edge_lcb':x.get('edge_lcb',0),'prediction_sigma':x.get('prediction_sigma',0)})
    realized_total+=realized
    def eqgross():
        eq=cash;gross=0.0
        for mid,x in pos.items():
            sh=finite(x['shares'],0);gross+=finite(x['cost'],0); c=cur.get(mid)
            if not c:eq+=sh*finite(x['entry_price'],0)*.98;continue
            _,y,n,z=c;b=y if x['side']=='YES' else n;w=walk_levels(b.bids,sh,buy=False);eq+=w.cash if w.depth_complete else sh*max(0,finite(b.bid(),0))*.98
        return eq,gross
    eq,gross=eqgross();peak=max(peak,eq);dd=max(0,1-eq/peak) if peak else 0;killed=killed or dd>=float(cfg.get('max_drawdown',.15));signals=opened=0;best=0.0
    if not killed and nlab>=80 and math.isfinite(sigma):
        ranked=[]
        for mid,(m,y,n,z) in cur.items():
            if mid in pos:continue
            pred=clamp(sum(beta[i]*z[0][i] for i in range(p)),-2.5*z[2],2.5*z[2]);fy=clamp(z[1]+pred,.001,.999);r,e=fee(clob,m,fc);ls=max(sigma,.25*z[3],.02*z[2])
            for side,b,fair in [('YES',y,fy),('NO',n,1-fy)]:
                room=min(a.max_trade_usd,float(cfg.get('max_market_fraction',.025))*max(eq,1),cash);sh=min(sum(q for _,q in b.asks),max(b.min_order,room/max(b.ask(),1e-6)))
                if sh<b.min_order:rej['min_order']+=1;continue
                c=taker_cost(asks=b.asks,shares=sh,fee_rate=r,fee_exponent=e,base_slippage_bps=a.base_slippage_bps,spread=b.spread(),short_vol=z[3],liquidity_score=z[4])
                if not c.depth_complete:rej['entry_depth']+=1;continue
                edge=robust_edge_lcb(fair_probability=fair,all_in_entry_price=c.all_in_price,prediction_sigma=ls,uncertainty_z=a.uncertainty_z)
                if edge>a.min_edge and sum(q for _,q in b.bids)>=sh:ranked.append((edge/max(ls,1e-6),edge,m,side,b,fair,ls,z))
        ranked.sort(reverse=True,key=lambda q:q[0]);signals=len(ranked);best=max((q[1] for q in ranked),default=0)
        for _,edge,m,side,b,fair,ls,z in ranked:
            if len(pos)>=a.max_positions or m.id in pos:continue
            eq,gross=eqgross();room=min(a.max_trade_usd,float(cfg.get('max_market_fraction',.025))*max(eq,1),cash,max(0,float(cfg.get('max_gross_fraction',.45))*max(eq,1)-gross));notional=mean_variance_notional(edge=edge,prediction_sigma=ls,max_notional=room,equity=max(eq,1),risk_budget_fraction=a.risk_budget_fraction)
            sh=notional/max(b.ask(),1e-6)
            if sh<b.min_order:continue
            r,e=fee(clob,m,fc);c=taker_cost(asks=b.asks,shares=sh,fee_rate=r,fee_exponent=e,base_slippage_bps=a.base_slippage_bps,spread=b.spread(),short_vol=z[3],liquidity_score=z[4])
            if not c.depth_complete:continue
            edge2=robust_edge_lcb(fair_probability=fair,all_in_entry_price=c.all_in_price,prediction_sigma=ls,uncertainty_z=a.uncertainty_z);cost=sh*c.all_in_price
            if edge2<=a.min_edge or cost>cash:continue
            cash-=cost;pos[m.id]={'side':side,'shares':sh,'entry_price':c.entry_vwap,'cost':cost,'entry_ts':now,'edge_lcb':edge2,'prediction_sigma':ls};opened+=1
            base.append_csv(a.run_dir/'fills.csv',['timestamp','market_id','slug','action','side','shares','price','fee','pnl','edge_lcb','prediction_sigma'],{'timestamp':now,'market_id':m.id,'slug':m.slug,'action':'BUY','side':side,'shares':sh,'price':c.entry_vwap+c.residual_slippage_per_share,'fee':c.fee_per_share*sh,'pnl':0,'edge_lcb':edge2,'prediction_sigma':ls})
    for mid,(m,y,n,z) in cur.items():
        samples.append({'ts':now,'market_id':mid,'mid':z[1],'spread':z[2],'x':z[0],'y':None});hh=hist.setdefault(mid,[]);hh.append([now,z[1]]);hist[mid]=[v for v in hh if now-int(finite(v[0],0))<=600][-180:]
    samples=samples[-30000:]; active={str(r.get('market_id')) for r in samples[-10000:] if r.get('market_id')};hist={k:v for k,v in hist.items() if k in active};eq,gross=eqgross();peak=max(peak,eq);dd=max(0,1-eq/peak) if peak else 0;killed=killed or dd>=float(cfg.get('max_drawdown',.15))
    state={'timestamp':now,'cash':cash,'equity':eq,'peak':peak,'drawdown':dd,'gross_exposure':gross,'killed':killed,'positions':pos,'samples':samples,'market_history':hist,'fee_cache':fc,'beta':beta,'prediction_sigma':sigma,'labeled_samples':nlab,'label_stats_last_tick':lab,'signals':signals,'opened':opened,'best_edge':best,'realized_pnl_last_tick':realized,'realized_pnl_total':realized_total,'rejection_histogram':dict(rej),'failures':fail,'model':'robust_ew_huber_l2_micro_v1'}
    base.atomic_json(sp,state);base.atomic_json(a.run_dir/'status.json',{k:state[k] for k in ['timestamp','cash','equity','peak','drawdown','gross_exposure','killed','labeled_samples','signals','opened','best_edge','realized_pnl_total','rejection_histogram','model']});print(json.dumps({'markets':len(markets),'equity':eq,'labeled':nlab,'signals':signals,'opened':opened,'best_edge':best,'killed':killed},sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
