from __future__ import annotations
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_london_launcher_has_no_training_or_retrospective_analytics():
    text=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    forbidden=['v7_external_rich_train.py','v7_external_residual_train.py','v7_maker_durable_learning.py','v7_pm_repricing_shadow.py','v7_generate_economic_artifacts.py','v7_profit_attribution.py','v7_profit_report.py','v7_economic_decision_report.py','v7_lossless_data_compaction.py','v7_permanent_evidence.py']
    for token in forbidden: assert token not in text, token
    assert 'v7_runtime_artifacts.py' in text
    assert 'v7_runtime_resource_plan.py' in text

def test_runtime_and_research_process_manifests_are_disjoint():
    runtime=json.loads((ROOT/'config/v7_process_manifest.json').read_text())
    research=json.loads((ROOT/'config/v7_research_process_manifest.json').read_text())
    rids={p['id'] for p in runtime['processes']}; qids={p['id'] for p in research['processes']}
    assert not (rids&qids)
    assert runtime['expected_process_count']==30 and runtime['expected_launcher_child_count']==29
    assert all(p.get('london_deployed') is True and p.get('runtime_class') in {'HOT_PATH','COLLECTOR','CONTROL'} for p in runtime['processes'])
    pure=[p for p in runtime['processes'] if str(p.get('id','')).startswith('pure_arb_') or p.get('id')=='venue_mode_source']
    assert len(pure)==13
    for p in pure:
        profile=runtime['profiles'][p['profile']]
        flags=dict(profile['authority_flags']); flags.update(p.get('authority_overrides') or {})
        assert not any(flags.values()), p['id']
    assert all(p.get('london_deployed') is False and p.get('runtime_class')=='RESEARCH_ONLY' for p in research['processes'])

def test_runtime_dependencies_have_no_research_stack():
    runtime=(ROOT/'requirements-runtime.txt').read_text().lower()
    for name in ['jupyter','matplotlib','optuna','scikit','sklearn','pandas']: assert name not in runtime
