#!/usr/bin/env python3
"""Fail-closed OOS/promotion gate for research-built London PAPER artifacts."""
from __future__ import annotations
import argparse,hashlib,json,math,re,time
from pathlib import Path

MAKER_SCHEMA='polymarket_v7_maker_execution_model_v1'
SCHEMA='polymarket_v7_candidate_validation_v1'
SHA40=re.compile(r'^[0-9a-f]{40}$')

def digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()

def finite(value)->bool:
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(float(value))

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--target-sha',required=True);ap.add_argument('--maker-model',type=Path,required=True);ap.add_argument('--rich-model',type=Path);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
    if not SHA40.fullmatch(a.target_sha): raise SystemExit('invalid target SHA')
    maker=json.loads(a.maker_model.read_text())
    if (maker.get('schema')!=MAKER_SCHEMA or maker.get('model_sha')!=a.target_sha
        or maker.get('paper_only') is not True or maker.get('authenticated_execution') is not False
        or maker.get('real_order_submission') is not False): raise SystemExit('maker candidate contract invalid')
    state=str(maker.get('model_state') or '')
    placement=maker.get('learned_placement_policy') if isinstance(maker.get('learned_placement_policy'),dict) else {}
    predictive=placement.get('predictive_oos_valid') is True
    if state=='COLD_START': maker_gate='BASELINE_COLD_START'; maker_promotable=True
    elif state=='EVIDENCE_ACCUMULATING' and predictive: maker_gate='PREDICTIVE_OOS_VALID'; maker_promotable=True
    else: maker_gate='EVIDENCE_NOT_OOS_VALID'; maker_promotable=False
    rich={'state':'UNAVAILABLE','promotable':True}
    if a.rich_model and a.rich_model.is_file():
        value=json.loads(a.rich_model.read_text()); scores=value.get('oos_scores') if isinstance(value.get('oos_scores'),dict) else {}; val=scores.get('validation') if isinstance(scores.get('validation'),dict) else {}
        ok=(value.get('artifact_role')=='RESEARCH' and value.get('code_sha')==a.target_sha
            and scores.get('audit_not_used_for_selection') is True
            and finite(val.get('brier')) and finite(val.get('logloss'))
            and (value.get('economic_replay') or {}).get('research_only') is True)
        rich={'state':'OOS_VALIDATED_RESEARCH' if ok else 'INVALID_RESEARCH_ARTIFACT','promotable':bool(ok),'sha256':digest(a.rich_model)}
    promotable=maker_promotable and rich['promotable']
    out={'schema':SCHEMA,'version':1,'timestamp_ns':time.time_ns(),'target_sha':a.target_sha,
         'paper_only':True,'execution_authority':False,'automatic_deployment':False,
         'maker':{'state':maker_gate,'promotable':maker_promotable,'model_state':state,'predictive_oos_valid':predictive,'sha256':digest(a.maker_model)},
         'rich_model':rich,'state':'PROMOTABLE' if promotable else 'REJECTED','promotable':promotable}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,sort_keys=True,indent=2)+'\n')
    print(json.dumps(out,sort_keys=True));return 0 if promotable else 2
if __name__=='__main__':raise SystemExit(main())
