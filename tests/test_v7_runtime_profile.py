from __future__ import annotations
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_runtime_profile import resolve

def test_paper_profile_is_single_safe_execution_surface():
    profile=ROOT/'config/runtime/paper.json'; v=json.loads(profile.read_text()); out=resolve(ROOT,profile)
    assert v['execution_mode']=='PAPER_SIMULATED'
    assert v['paper_only'] is True and v['authenticated_execution'] is False and v['real_order_submission'] is False
    assert out['PM_V7_EXECUTION_MODE']=='PAPER_SIMULATED'
    assert set(out)-{'PM_V7_EXECUTION_MODE'}==set(v['paths'])

def test_launcher_uses_profile_not_policy_env_soup():
    s=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    assert 'PM_V7_RUNTIME_PROFILE' in s and 'v7_runtime_profile.py' in s
    for old in ['PM_V7_MAKER_POLICY','PM_V7_EXTERNAL_FAIR_POLICY','PM_V7_LEAD_LAG_TAKER_CONFIG','PM_V7_CRYPTO_UNIVERSE_CONFIG']:
        assert old not in s
    assert 'export PM_V7_EXECUTION_MODE' in s


def test_ci_profile_uses_explicit_four_cpu_smoke_plan_only():
    profile=ROOT/'config/runtime/ci-paper.json'; v=json.loads(profile.read_text()); out=resolve(ROOT,profile)
    assert v['name']=='ci-paper-smoke'
    assert v['paths']['RUNTIME_RESOURCE_CONFIG']=='config/v7_runtime_resources_ci.json'
    resources=json.loads((ROOT/v['paths']['RUNTIME_RESOURCE_CONFIG']).read_text())
    assert resources['minimum_visible_cpus']==4
    assert sum(int(resources[k]['reserved_cores']) for k in ('hot_path','collector','control','housekeeping'))==4
    production=json.loads((ROOT/'config/v7_runtime_resources.json').read_text())
    assert production['minimum_visible_cpus']==8

def test_live_validation_selects_ci_resource_profile():
    workflow=(ROOT/'.github/workflows/v7-live-paper-validation.yml').read_text()
    assert 'PM_V7_RUNTIME_PROFILE=config/runtime/ci-paper.json' in workflow
