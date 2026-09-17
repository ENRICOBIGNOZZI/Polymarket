from __future__ import annotations
import json,subprocess,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SHA='a'*40

def maker(state='COLD_START', predictive=False):
    return {'schema':'polymarket_v7_maker_execution_model_v1','model_sha':SHA,'paper_only':True,
            'authenticated_execution':False,'real_order_submission':False,'model_state':state,
            'learned_placement_policy':{'predictive_oos_valid':predictive}}

def run(model):
    with tempfile.TemporaryDirectory() as d:
        d=Path(d); m=d/'maker.json'; o=d/'validation.json';m.write_text(json.dumps(model))
        r=subprocess.run(['python3',str(ROOT/'research/validate_candidate.py'),'--target-sha',SHA,'--maker-model',str(m),'--output',str(o)],capture_output=True,text=True)
        return r.returncode,json.loads(o.read_text())

def test_cold_start_baseline_is_explicitly_promotable_without_claiming_oos_model():
    code,v=run(maker());assert code==0;assert v['state']=='PROMOTABLE';assert v['maker']['state']=='BASELINE_COLD_START';assert v['automatic_deployment'] is False

def test_evidence_accumulating_without_predictive_oos_is_rejected():
    code,v=run(maker('EVIDENCE_ACCUMULATING',False));assert code==2;assert v['state']=='REJECTED';assert v['maker']['state']=='EVIDENCE_NOT_OOS_VALID'

def test_predictively_valid_maker_is_promotable():
    code,v=run(maker('EVIDENCE_ACCUMULATING',True));assert code==0;assert v['maker']['state']=='PREDICTIVE_OOS_VALID'

def test_research_cycle_validates_before_push():
    build=(ROOT/'research/build_runtime_artifacts.sh').read_text(); push=(ROOT/'research/push_runtime_artifacts.sh').read_text(); cycle=(ROOT/'research/run_research_cycle.sh').read_text()
    assert build.index('validate_candidate.py') < build.index('runtime_artifact_manifest.py')
    assert 'candidate_validation.json' in push and "state')=='PROMOTABLE'" in push
    assert cycle.index('build_runtime_artifacts.sh') < cycle.index('push_runtime_artifacts.sh')
