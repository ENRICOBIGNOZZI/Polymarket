from __future__ import annotations
import json,subprocess,tempfile
from pathlib import Path
from scripts.v7_model_runtime_approval import maker_economic_hash
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

def test_manual_artifacts_validate_and_daily_cycle_never_pushes():
    build=(ROOT/'research/build_runtime_artifacts.sh').read_text(); push=(ROOT/'research/push_runtime_artifacts.sh').read_text(); cycle=(ROOT/'research/run_research_cycle.sh').read_text()
    assert build.index('validate_candidate.py') < build.index('runtime_artifact_manifest.py')
    assert 'candidate_validation.json' in push and "state')=='PROMOTABLE'" in push
    assert 'v7-model-runtime-approval.json' in push
    assert 'v7_model_runtime_approval.py' in push
    assert '--reviewed-report "$REVIEWED_REPORT"' in push
    assert 'model_runtime_approval.json' in push
    assert 'reviewed_backtest_report' in push
    assert 'research.learning.daily' in cycle
    assert 'push_runtime_artifacts.sh' not in cycle


def test_runtime_model_approval_defaults_fail_closed():
    approval=json.loads((ROOT/'deploy/v7-model-runtime-approval.json').read_text())
    assert approval['schema']=='polymarket_v7_model_runtime_approval_v1'
    assert approval['approved'] is False
    assert approval['explicit_user_approval_required'] is True
    assert approval['review_backtest_before_approval'] is True
    assert approval['automatic_promotion'] is False
    assert approval['target_model_sha'] is None
    assert approval['bundle_id'] is None
    assert approval['backtest_report_path'] is None
    assert approval['backtest_report_sha256'] is None
    assert approval['economic_model_identity_sha256'] is None

def test_economic_model_identity_ignores_provenance_but_not_policy_mapping():
    left=maker()
    left.update({
        'code_sha':SHA,'version':1,'generated_ts_ms':1,
        'training_source_model_shas':[SHA],
        'training_window':{'records':10},'validation_window':None,
    })
    right=json.loads(json.dumps(left))
    right.update({
        'model_sha':'b'*40,'code_sha':'b'*40,'version':999,'generated_ts_ms':999,
        'training_source_model_shas':['b'*40],
        'training_window':{'records':999},'validation_window':{'x':1},
    })
    assert maker_economic_hash(left)==maker_economic_hash(right)
    right['learned_placement_policy']={'predictive_oos_valid':True,'coefficient':1.0}
    assert maker_economic_hash(left)!=maker_economic_hash(right)

def test_london_cutover_requires_reviewed_approval_only_for_changed_economic_model():
    cutover=(ROOT/'ops/v7_london_cutover.sh').read_text()
    assert 'TARGET_MODEL_IDENTITY' in cutover
    assert 'ACTIVE_MODEL_IDENTITY' in cutover
    assert 'v7_model_runtime_approval.py' in cutover
    assert 'EXPLICIT_REVIEWED_APPROVAL_VERIFIED' in cutover
    assert 'NOT_REQUIRED_SAME_ECONOMIC_MODEL' in cutover
    assert 'reviewed_backtest_report' in cutover
