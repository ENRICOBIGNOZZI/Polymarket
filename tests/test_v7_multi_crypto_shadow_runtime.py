#!/usr/bin/env python3
from __future__ import annotations
import json,sys,tempfile
from pathlib import Path
ROOT=Path('/Users/enrico/polymarket-multi-crypto-v7'); sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_shadow_runtime import ASSETS,external_commands,validate_policy,venue_symbol
SHA='a'*40

def policy():return validate_policy(json.loads((ROOT/'config/v7_multi_crypto_shadow_runtime.json').read_text()))
def assets():return json.loads((ROOT/'config/v7_multi_crypto_assets.json').read_text())

def test_policy_is_strictly_zero_authority():
    p=policy(); assert p['paper_only'] is True and p['execution_authority'] is False and p['automatic_promotion'] is False
    raw=dict(p); raw['real_order_submission']=True
    try:validate_policy(raw)
    except ValueError:pass
    else:raise AssertionError('real order authority accepted')

def test_external_commands_cover_exactly_six_assets_and_public_data_only():
    commands=external_commands(assets(),Path('/bin/external'),Path('/tmp/shadow'),SHA)
    assert set(commands)==set(ASSETS)
    for asset,command in commands.items():
        text=' '.join(command); assert '--model-sha '+SHA in text and '--asset '+asset in text
        assert 'private' not in text.lower() and 'order-submission' not in text.lower()

def test_optional_venues_are_explicit_none_not_btc_fallback():
    rows={row['asset']:row for row in assets()['assets']}
    assert venue_symbol(rows['BNB'],'coinbase_spot')=='NONE'
    assert venue_symbol(rows['BNB'],'deribit')=='NONE'
    assert venue_symbol(rows['DOGE'],'deribit')=='NONE'

def test_runtime_source_has_no_execution_or_ledger_owner():
    source=(ROOT/'scripts/v7_multi_crypto_shadow_runtime.py').read_text()
    forbidden=('v7_global_portfolio_coordinator','v7_execution_ledger','v7_lead_lag_taker_runtime','authorized_maker_paper_executor','real_order_submission=true')
    assert all(term not in source for term in forbidden)
    for role in ('oracle_hub','pm_book_hub','pm_label_hub','feature_engine','feature_tape'):assert role in source

def test_config_requires_compact_label_tape_and_loopback_proxy():
    p=policy(); assert p['compact_label_tape'] is True and p['public_proxy_host']=='127.0.0.1'
    bad=dict(p); bad['public_proxy_host']='0.0.0.0'
    try:validate_policy(bad)
    except ValueError:pass
    else:raise AssertionError('non-loopback proxy accepted')

if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,f in tests:f()
    print(f'{len(tests)} function tests passed')
