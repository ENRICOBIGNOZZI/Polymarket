#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, json, math, os, time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Mapping


def finite(value: Any, default: float = 0.0) -> float:
    try: out=float(value)
    except (TypeError,ValueError,OverflowError): return default
    return out if math.isfinite(out) else default

def atomic_json(path:Path,value:Any)->None:
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n',encoding='utf-8');os.replace(tmp,path)
def read_json(path:Path):
    try:
        v=json.loads(path.read_text(encoding='utf-8'));return v if isinstance(v,dict) else None
    except Exception:return None
def read_last_csv(path:Path):
    try:
        with path.open(newline='',encoding='utf-8') as h:rows=list(csv.DictReader(h))
        return rows[-1] if rows else None
    except Exception:return None

@dataclass(frozen=True)
class SleeveRisk:
    name:str;equity:float;gross:float;killed:bool;timestamp:int;stale:bool;source:str

def _json_sleeve(name,path,now,stale_seconds,fallback):
    r=read_json(path)
    if r is None:return None
    ts=int(finite(r.get('timestamp'),0));eq=finite(r.get('equity'),finite(r.get('cash'),fallback));gross=max(0,finite(r.get('gross_exposure'),0));stale=ts<=0 or now-ts>stale_seconds or ts>now+30
    return SleeveRisk(name,eq,gross,bool(r.get('killed',False)),ts,stale,str(path))

def load_sleeves(root:Path,alloc:Mapping[str,float],total:float,now:int,stale_seconds:int):
    out={};r=read_last_csv(root/'maker'/'maker_equity.csv')
    if r:
        ts=int(finite(r.get('timestamp'),0));out['maker']=SleeveRisk('maker',finite(r.get('equity'),total*alloc.get('maker',0)),max(0,finite(r.get('reserved_cash'),0))+max(0,finite(r.get('equity'),0)-finite(r.get('cash'),0)),str(r.get('killed','0')) in {'1','true','True'},ts,ts<=0 or now-ts>stale_seconds or ts>now+30,str(root/'maker'/'maker_equity.csv'))
    else:out['maker']=None
    r=read_last_csv(root/'multileg_equity.csv')
    if r:
        ts=int(finite(r.get('timestamp'),0));out['broker']=SleeveRisk('broker',finite(r.get('equity'),total*alloc.get('broker',0)),max(0,finite(r.get('reserved_cash'),0))+max(0,finite(r.get('gross_entry_cash'),0)),str(r.get('killed','0')) in {'1','true','True'},ts,ts<=0 or now-ts>stale_seconds or ts>now+30,str(root/'multileg_equity.csv'))
    else:out['broker']=None
    out['micro_taker']=_json_sleeve('micro_taker',root/'micro_taker'/'status.json',now,stale_seconds,total*alloc.get('micro_taker',0))
    out['hard_arb']=_json_sleeve('hard_arb',root/'hard_arb'/'status.json',now,stale_seconds,total*alloc.get('hard_arb',0))
    ext=_json_sleeve('external',root/'external'/'status.json',now,stale_seconds,total*alloc.get('external',0))
    if ext is None:
        r=read_last_csv(root/'external'/'broker_state.csv')
        if r:
            ts=int(finite(r.get('timestamp'),0));ext=SleeveRisk('external',finite(r.get('equity'),finite(r.get('cash'),total*alloc.get('external',0))),max(0,finite(r.get('gross_exposure'),finite(r.get('open_notional'),0))),str(r.get('killed',r.get('kill_switch','0'))) in {'1','true','True'},ts,ts<=0 or now-ts>stale_seconds or ts>now+30,str(root/'external'/'broker_state.csv'))
    out['external']=ext;return out

def evaluate_global_risk(*,total_capital:float,reserve_fraction:float,expected_allocations:Mapping[str,float],sleeves:Mapping[str,SleeveRisk|None],shock_multipliers:Mapping[str,float],previous_peak:float,max_drawdown:float,max_gross_fraction:float,max_scenario_loss_fraction:float,within_startup_grace:bool):
    total=max(1,total_capital);missing=sorted(k for k,v in sleeves.items() if v is None);stale=sorted(k for k,v in sleeves.items() if v is not None and v.stale);child=sorted(k for k,v in sleeves.items() if v is not None and v.killed);observed=sum(max(0,v.equity) for v in sleeves.values() if v is not None)
    if within_startup_grace:
        fallback=sum(total*max(0,finite(expected_allocations.get(k),0)) for k,v in sleeves.items() if v is None);equity=observed+fallback+total*reserve_fraction
    else:equity=observed+total*reserve_fraction
    peak=max(total,previous_peak,equity);dd=max(0,1-equity/peak) if peak else 1;gross=sum(max(0,v.gross) for v in sleeves.values() if v is not None);scenario=sum(max(0,v.gross)*max(0,finite(shock_multipliers.get(k),1)) for k,v in sleeves.items() if v is not None);reasons=[]
    if dd>=max_drawdown:reasons.append('global_drawdown')
    if gross>max_gross_fraction*total+1e-9:reasons.append('global_gross')
    if scenario>max_scenario_loss_fraction*total+1e-9:reasons.append('scenario_loss')
    if child:reasons.append('child_kill:'+','.join(child))
    if not within_startup_grace and missing:reasons.append('missing_sleeve:'+','.join(missing))
    if not within_startup_grace and stale:reasons.append('stale_sleeve:'+','.join(stale))
    return {'equity':equity,'peak_equity':peak,'drawdown':dd,'gross_exposure':gross,'gross_fraction':gross/total,'scenario_loss':scenario,'scenario_loss_fraction':scenario/total,'missing_sleeves':missing,'stale_sleeves':stale,'child_killed':child,'kill':bool(reasons),'kill_reasons':reasons}

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,required=True);ap.add_argument('--run-root',type=Path,required=True);a=ap.parse_args();cfg=json.loads(a.config.read_text());v6=cfg.get('v6') if isinstance(cfg.get('v6'),dict) else {};risk=v6.get('global_risk') if isinstance(v6.get('global_risk'),dict) else {};total=max(1,finite(cfg.get('starting_capital'),10000));alloc={'maker':finite(v6.get('micro_maker_capital_fraction'),.12),'micro_taker':finite(v6.get('micro_taker_capital_fraction'),.08),'broker':finite(v6.get('relative_value_capital_fraction'),.5),'hard_arb':finite(v6.get('hard_arb_capital_fraction'),.15),'external':finite(v6.get('external_capital_fraction'),.1)};reserve=max(0,finite(v6.get('reserve_fraction'),.05));now=int(time.time());a.run_root.mkdir(parents=True,exist_ok=True);sp=a.run_root/'global_risk_state.json';old=read_json(sp) or {};first=int(finite(old.get('first_ts'),now));grace=max(0,int(finite(risk.get('startup_grace_seconds'),180)));stale=max(30,int(finite(risk.get('stale_seconds'),180)));within=now-first<grace;sleeves=load_sleeves(a.run_root,alloc,total,now,stale);raw=risk.get('shock_multipliers') if isinstance(risk.get('shock_multipliers'),dict) else {};defaults={'maker':.45,'micro_taker':.65,'broker':.4,'hard_arb':.1,'external':1};shocks={k:finite(raw.get(k),v) for k,v in defaults.items()};res=evaluate_global_risk(total_capital=total,reserve_fraction=reserve,expected_allocations=alloc,sleeves=sleeves,shock_multipliers=shocks,previous_peak=max(total,finite(old.get('peak_equity'),total)),max_drawdown=max(0,finite(cfg.get('max_drawdown'),.15)),max_gross_fraction=max(0,finite(risk.get('max_gross_fraction'),finite(cfg.get('max_gross_fraction'),.45))),max_scenario_loss_fraction=max(0,finite(risk.get('max_scenario_loss_fraction'),.12)),within_startup_grace=within);prev=bool(old.get('killed',False)) or (a.run_root/'global_kill.flag').exists();killed=prev or res['kill'];reasons=list(old.get('kill_reasons',[])) if prev and isinstance(old.get('kill_reasons'),list) else []
    for x in res['kill_reasons']:
        if x not in reasons:reasons.append(x)
    state={'schema':'polymarket_v6_global_scenario_risk_v1','timestamp':now,'first_ts':first,'paper_only':True,'authenticated_execution':False,'startup_grace':within,'startup_grace_seconds':grace,'stale_seconds':stale,'killed':killed,'kill_reasons':reasons,**{k:v for k,v in res.items() if k not in {'kill','kill_reasons'}},'sleeves':{k:(asdict(v) if v else None) for k,v in sleeves.items()},'shock_multipliers':shocks};atomic_json(sp,state);atomic_json(a.run_root/'global_risk_status.json',state)
    if killed and not (a.run_root/'global_kill.flag').exists():(a.run_root/'global_kill.flag').write_text(json.dumps({'timestamp':now,'reasons':reasons},sort_keys=True)+'\n')
    print(json.dumps({k:state[k] for k in ['timestamp','equity','drawdown','gross_fraction','scenario_loss_fraction','startup_grace','killed','kill_reasons']},sort_keys=True));return 2 if killed else 0

if __name__=='__main__':raise SystemExit(main())
