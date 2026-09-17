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
