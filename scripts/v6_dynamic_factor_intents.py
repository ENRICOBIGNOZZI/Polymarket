#!/usr/bin/env python3
from __future__ import annotations
import json, math, os, sys, urllib.parse
import v6_local_factor_intents as base
from v6_execution_model import clamp, state_slippage_bps


def weights(n:int,h:float=72)->list[float]:
    r=[math.exp(-math.log(2)*(n-1-i)/max(1,h)) for i in range(n)];s=sum(r);return [x/s for x in r]
def mean(x,w):return sum(a*b for a,b in zip(x,w))
def cov(x,y,w):
    mx,my=mean(x,w),mean(y,w);return sum(z*(a-mx)*(b-my) for z,a,b in zip(w,x,y))

def dynamic_factor_panel(levels:dict[str,list[float]],half_life:float=72):
    if len(levels)<3:return [],{},{},{}
    n=min(map(len,levels.values()))
    if n<4:return [],{},{},{}
    levels={k:v[-n:] for k,v in levels.items()};rets={k:[v[i]-v[i-1] for i in range(1,n)] for k,v in levels.items()};w=weights(n-1,half_life);z={};sc={}
    for k,r in rets.items():
        mu=mean(r,w);sd=math.sqrt(max(0,cov(r,r,w)))
        if sd>1e-8:z[k]=[(x-mu)/sd for x in r];sc[k]=sd
    if len(z)<3:return [],{},{},{}
    keys=sorted(z);f=[sum(z[k][j] for k in keys)/len(keys) for j in range(n-1)];load={}
    for _ in range(3):
        fv=max(1e-10,cov(f,f,w));load={k:cov(z[k],f,w)/fv for k in keys};den=sum(a*a for a in load.values() if abs(a)>=.03)
        if den<=1e-10:return [],{},{},{}
        f=[sum(load[k]*z[k][j] for k in keys if abs(load[k])>=.03)/den for j in range(n-1)]
    state={}
    for k in keys:
        cur=[0.0]
        for j in range(n-1):cur.append(cur[-1]+z[k][j]-load[k]*f[j])
        state[k]=cur
    return f,load,state,sc

def dynamic_candidates(key,ms,series,min_common,min_z):
    usable=[m for m in ms if m.market_id in series and len(series[m.market_id])>=min_common]
    if len(usable)<3:return []
    common=set(series[usable[0].market_id])
    for m in usable[1:]:common&=set(series[m.market_id])
    times=sorted(common)
    if len(times)<min_common:return []
    levels={m.market_id:[series[m.market_id][t] for t in times] for m in usable};_,load,resid,sc=dynamic_factor_panel(levels)
    by={m.market_id:m for m in usable};out=[]
    for mid,r in resid.items():
        if abs(load.get(mid,0))<.05:continue
        phi,t,mu,sd=base.ar_fit(r)
        if not(.02<phi<.999 and t<0 and sd>1e-8):continue
        rz=(r[-1]-mu)/sd
        if abs(rz)<min_z:continue
        out.append(base.Candidate(key,by[mid],rz,phi,t,base.pvalue_from_t(t),load[mid],max(1e-8,sc[mid]),(phi-1)*(r[-1]-mu),len(times)))
    return out

def fee_rate(clob,token,default,cache):
    if token in cache:return cache[token]
    rate=default
    try:
        raw=base.request_json(clob+'/fee-rate?token_id='+urllib.parse.quote(token))
        if isinstance(raw,dict) and raw.get('base_fee') is not None:rate=max(0,base.finite(raw['base_fee'],default*10000)/10000)
    except Exception:pass
    cache[token]=rate;return rate

def dynamic_pair(key,signals,books,now,min_edge,max_trade,default_rate,fee_exp,slip_bps,serial):
    best=None
    for i,a in enumerate(signals):
        for b in signals[i+1:]:
            if a.residual_z*b.residual_z>=0:continue
            sa='NO' if a.residual_z>0 else 'YES';sb='NO' if b.residual_z>0 else 'YES';ea=(-1 if sa=='NO' else 1)*a.loading;eb=(-1 if sb=='NO' else 1)*b.loading
            if ea*eb>=0 or abs(eb)<1e-8:continue
            score=abs(a.residual_z*a.tstat)+abs(b.residual_z*b.tstat)
            if best is None or score>best[0]:best=(score,a,b,sa,sb,abs(ea/eb))
    if best is None:return []
    _,a,b,sa,sb,wb=best
    if not .05<=wb<=10:return []
    cfg=getattr(base,'_dynamic_runtime',{});clob=cfg.get('clob','');cache=cfg.setdefault('fee_cache',{});cap=pnl=0.0;units=math.inf;mins=0.0;halves=[]
    legs=[(a,sa,1.0),(b,sb,wb)]
    for s,side,w in legs:
        y,n=books.get(s.market.yes),books.get(s.market.no)
        if y is None or n is None:return []
        bk=y if side=='YES' else n;future_yes=base.logistic(base.logit(y.mid)+s.expected_residual_change*s.yes_sd);future=future_yes if side=='YES' else 1-future_yes;token=s.market.yes if side=='YES' else s.market.no
        fr=fee_rate(clob,token,default_rate,cache);liq=clamp(math.log1p(s.market.liquidity)/math.log1p(100000),0,1);recent=max(.0001,abs(s.expected_residual_change*s.yes_sd));bps=state_slippage_bps(base_bps=slip_bps,spread=bk.spread,short_vol=recent,participation=.25,liquidity_score=liq)
        ex=clamp(future-.5*bk.spread,.001,.999)*(1-bps/10000);pps=ex-base.fee_per_share(ex,fr,fee_exp)-bk.bid
        if pps<=0:return []
        cap+=w*bk.bid;pnl+=w*pps;units=min(units,bk.bid_size/w);mins=max(mins,bk.min_order/w)
        if 0<s.phi<1:halves.append(-math.log(2)/math.log(s.phi))
    edge=pnl/max(cap,1e-9)
    if edge<=min_edge or not math.isfinite(units) or units<mins:return []
    maxn=min(max_trade,units*cap)
    if maxn<=0:return []
    bundle=f'DYNAMIC_FACTOR-{now}-{serial}';hold=now+int(max(1,min(24,2*max(halves,default=2)))*3600);rows=[]
    for s,side,w in legs:
        bk=books[s.market.yes if side=='YES' else s.market.no];rows.append({'bundle_id':bundle,'strategy':'DYNAMIC_LOCAL_FACTOR','event_id':key,'created_ts':now,'mode':'MAKER','expected_edge':edge,'max_notional':maxn,'market_id':s.market.market_id,'side':side,'weight':w,'limit_price':bk.bid,'execution_deadline_ts':now+180,'hold_deadline_ts':hold})
    return rows

def main():
    cfg_path=sys.argv[sys.argv.index('--config')+1] if '--config' in sys.argv else 'config/paper_v6.json';cfg=json.loads(open(cfg_path).read());base._dynamic_runtime={'clob':cfg['clob_url'],'fee_cache':{}};base.local_candidates=dynamic_candidates;base.build_pair_intent=dynamic_pair;rc=base.main()
    if '--status' in sys.argv:
        p=sys.argv[sys.argv.index('--status')+1]
        try:
            s=json.loads(open(p).read());s['model']='dynamic_ew_return_factor_v1';tmp=p+'.tmp';open(tmp,'w').write(json.dumps(s,indent=2,sort_keys=True)+'\n');os.replace(tmp,p)
        except Exception:pass
    return rc

if __name__=='__main__':raise SystemExit(main())
