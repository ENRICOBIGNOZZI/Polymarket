from __future__ import annotations
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_slow_context import build_context,context_path,publish_contexts,FIELDS


def ext():
    return {'schema':'polymarket_v7_external_venue_runtime_v1','code_sha':'a'*40,'asset':'BTC',
      'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
      'valid':True,'state_version':7,'price_inputs_receive_monotonic_ns':100,
      'price_inputs_valid_until_monotonic_ns':1000,'composite_price':42000.,
      'realized_vol_slow':.2,'realized_vol_medium':.3,'dispersion_bps':2.,
      'return_history_available':{'5s':False},'return_5s':0.0}


def build(external=None,oracle=None,now=200,asset='BTC',market='m'):
    return build_context(external or {},oracle or {},code_sha='a'*40,run_id='r',
      market={'asset':asset,'horizon':'M5','market_id':market,'close_timestamp_unix':300},
      now_ns=now,wall_ns=200_000_000_000)


def test_missing_stays_null_and_identity_is_bound():
    value=build();assert value['fields']==dict.fromkeys(FIELDS)
    assert value['observation_only'] and not value['real_order_submission']
    assert value['market_id']=='m' and value['run_id']=='r'
    assert 'NaN' not in json.dumps(value,allow_nan=False)


def test_heartbeat_does_not_extend_source_lifetime():
    first=build(ext());later=build(ext(),now=900)
    assert first['fields']['spot_composite']==later['fields']['spot_composite']
    assert build(ext(),now=1001)['fields']['spot_composite'] is None
    assert first['fields']['return_5s'] is None
    assert build(ext(),asset='ETH')['fields']['spot_composite'] is None


def test_derivative_uses_its_own_field_clock_and_units():
    source=ext();source['derivative_contexts']=[{'venue':'BINANCE_USDM','healthy':True,'gap':False,
      'valid_mask':4,'field_receive_monotonic_ns':[10,10,100,10],
      'funding_rate':.002,'open_interest_native':55}]
    f=build(source)['fields'];assert f['binance_funding']['value']==.002
    assert f['binance_funding']['receive_monotonic_ns']==100
    assert f['binance_open_interest'] is None
    source['derivative_contexts'][0].pop('field_receive_monotonic_ns')
    assert build(source)['fields']['binance_funding'] is None


def test_oracle_cannot_cross_market_or_asset_binding():
    oracle={'code_sha':'a'*40,'market':{'market_id':'m'},'paper_only':True,
      'authenticated_execution':False,'real_order_submission':False,
      'contract':{'verified':True,'rules_hash_recognized':True},
      'oracle':{'healthy':True,'value':42.,'receive_monotonic_ns':100,'source_sequence':1},
      'settlement_reference':{'valid':True,'value':40.,'receive_monotonic_ns':90,'version':1}}
    assert build(oracle=oracle)['fields']['oracle_value']['value']==42.
    assert build(oracle=oracle,market='other')['fields']['oracle_value'] is None
    assert build(oracle=oracle,asset='ETH')['fields']['opening_reference'] is None


def test_nonfinite_future_and_bool_do_not_become_values():
    for bad in (float('nan'),float('inf'),True,None):
        source=ext();source['composite_price']=bad
        assert build(source)['fields']['spot_composite'] is None
    source=ext();source['price_inputs_receive_monotonic_ns']=201
    assert build(source)['fields']['spot_composite'] is None


def test_publication_has_no_execution_or_path_authority(tmp_path):
    markets={'BTC:M5':{'market_id':'../outside','asset':'BTC','horizon':'M5'}}
    assert publish_contexts(tmp_path,code_sha='a'*40,run_id='r',markets=markets)==1
    target=context_path(tmp_path,'../outside')
    assert target.parent==tmp_path/'control'/'slow_context'
    assert json.loads(target.read_text())['fields']==dict.fromkeys(FIELDS)
    assert not (tmp_path/'ledger').exists()
