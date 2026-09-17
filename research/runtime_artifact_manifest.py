#!/usr/bin/env python3
"""Create/validate the immutable artifact bundle consumed by London."""
from __future__ import annotations
import argparse,hashlib,json,re,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'scripts'))
from v7_fair_model_artifact import FairModelArtifact, ArtifactError
SCHEMA='polymarket_v7_runtime_artifact_bundle_v1'; MAKER_SCHEMA='polymarket_v7_maker_execution_model_v1'

def digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1<<20),b''):h.update(block)
    return h.hexdigest()

def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

def build(target_sha:str,maker:Path,rich:Path|None,candidate:Path)->dict:
    if not re.fullmatch(r'[0-9a-f]{40}',target_sha): raise ValueError('target_sha')
    m=json.loads(maker.read_text())
    cv=json.loads(candidate.read_text())
    if cv.get('schema')!='polymarket_v7_candidate_validation_v1' or cv.get('target_sha')!=target_sha or cv.get('state')!='PROMOTABLE' or cv.get('promotable') is not True or cv.get('automatic_deployment') is not False: raise ValueError('candidate_validation')
    if m.get('schema')!=MAKER_SCHEMA or m.get('model_sha')!=target_sha or m.get('paper_only') is not True or m.get('authenticated_execution') is not False or m.get('real_order_submission') is not False: raise ValueError('maker_contract')
    rich_meta={'state':'UNAVAILABLE','path':'rich_research_model.json','sha256':None,'model_hash':None}
    if rich and rich.is_file():
        raw=json.loads(rich.read_text()); a=FairModelArtifact(**raw); a.validate()
        if a.code_sha!=target_sha or a.artifact_role!='RESEARCH': raise ValueError('rich_target_sha')
        rich_meta={'state':'AVAILABLE','path':'rich_research_model.json','sha256':digest(rich),'model_hash':a.model_hash}
    value={'schema':SCHEMA,'version':1,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'target_model_sha':target_sha,
      'created_at_ns':time.time_ns(),'runtime_training':False,'automatic_deployment':False,'candidate_validation':{'path':'candidate_validation.json','sha256':digest(candidate)},'maker_execution_model':{'path':'maker_execution_model.json','sha256':digest(maker),'model_state':m.get('model_state'),'policy_hash':m.get('policy_hash'),'config_hash':m.get('config_hash')},'rich_research_model':rich_meta}
    value['bundle_id']=hashlib.sha256(canonical({k:v for k,v in value.items() if k not in {'created_at_ns','bundle_id'}})).hexdigest()
    return value

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--target-sha',required=True); ap.add_argument('--maker-model',type=Path,required=True); ap.add_argument('--rich-model',type=Path); ap.add_argument('--candidate-validation',type=Path,required=True); ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args(); v=build(a.target_sha,a.maker_model,a.rich_model,a.candidate_validation); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(v,sort_keys=True,indent=2)+'\n'); print(json.dumps({'bundle_id':v['bundle_id'],'target_model_sha':v['target_model_sha'],'rich_model_state':v['rich_research_model']['state']},sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
