import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_preregistered_economic_sweeps_are_exact_and_zero_authority():
    p=json.loads((ROOT/'research/economic/protocol.json').read_text())
    assert p['paper_only'] is True
    assert p['execution_authority'] is False
    assert p['automatic_promotion'] is False
    assert p['real_order_submission'] is False
    assert p['quantity_shares']==[5,10,20,50,100]
    assert p['tte_windows_seconds']==[[0,30],[30,60],[60,105],[105,120],[120,180],[180,300]]
    assert p['baseline_tte_window_seconds']==[105,120]
    assert p['entry_caps_per_market']==[1,2,4]
    assert p['multi_entry_requires_distinct_signal'] is True
    assert p['entry_cooldown_ms']==[100,250,500,1000]
    assert p['capacity_liquidity_semantics']=='CONSUME_VISIBLE_RECEIVE_TIME_DEPTH_ONCE_PER_SCENARIO_NO_REUSE'
    assert p['multi_entry_semantics']=='DISTINCT_SIGNAL_VERSION_PLUS_COOLDOWN'
