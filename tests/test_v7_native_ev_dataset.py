from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'research/economic'))
from native_ev_dataset import (
    build_rows, first_accepted_taker_per_market, settlement_labels_from_ledger,
)

SHA='a'*40

def obs(market='m1',seq=1,accepted=True,decision=100_000_000_000,
        signal=1,asset='SOL',horizon='M5'):
    return {
        'schema':'polymarket_v7_native_observation_v1','kind':2,'accepted':accepted,
        'book_valid':True,'market_id':market,'token_id':'yes','asset':asset,'horizon':horizon,
        'sequence':seq,'decision_wall_ns':decision,'signal_version':signal,'book_version':7,
        'bid_e4':1900,'ask_e4':2000,'bid_quantity':30_000_000,'ask_quantity':10_000_000,
        'binance_return_100ms_bp':1.2,'coinbase_return_100ms_bp':.3,'direction':1,
        'confirmed_non_opposing':True,'tte_ns':60_000_000_000,
        'signal_age_ns':25_000_000,'fee_rate':.07,'fee_exponent':1.0,
    }

def label(outcome=1, observed=200_000_000_000):
    return {'token_payouts':{'yes':outcome,'no':1-outcome},
            'observed_ns':observed,'record_id':'final-1'}

def test_first_accepted_taker_is_one_per_market():
    rows=[obs(seq=4,decision=140),obs(seq=2,decision=120),obs(seq=1,accepted=False,decision=110)]
    chosen=first_accepted_taker_per_market(rows)
    assert len(chosen)==1 and chosen[0]['sequence']==2

def test_build_rows_uses_mid_prior_ask_cost_and_exact_fee():
    rows,stats=build_rows([obs()],{'m1':label(1)})
    assert stats['selected_markets']==1 and stats['missing_labels']==0
    row=rows[0]
    assert row['pm_probability']==.195
    assert row['executable_ask']==.2
    assert row['outcome']==1
    assert row['fee_per_share']==.07*(.2*.8)
    assert row['break_even_probability']==.2+.07*(.2*.8)
    assert row['features']['tte_seconds']==60
    assert row['features']['signal_age_ms']==25
    assert row['features']['depth_imbalance']==.5
    assert row['features']['aligned_signal_return_bp']==1.2
    assert row['features']['aligned_confirmation_return_bp']==.3
    assert row['features']['asset_SOL']==1 and row['features']['horizon_M5']==1
    assert row['features']['confirmed_non_opposing']==1
    assert row['features']['aligned_signal_return_bp']==1.2
    assert row['features']['aligned_confirmation_return_bp']==.3

def test_missing_and_fifty_fifty_labels_are_not_zero():
    rows,stats=build_rows([obs()],{})
    assert rows==[] and stats['missing_labels']==1
    rows,stats=build_rows([obs()],{'m1':label(.5)})
    assert rows==[] and stats['nonbinary_labels']==1

def test_invalid_or_rejected_book_never_enters_training():
    bad=obs();bad['book_valid']=False
    rows,_=build_rows([bad,obs(market='m2',accepted=False)],{'m1':label(),'m2':label()})
    assert rows==[]

def test_wall_clock_prevents_monotonic_label_comparison_bug():
    bad=obs(decision=300_000_000_000)
    rows,stats=build_rows([bad],{'m1':label(1,200_000_000_000)})
    assert rows==[] and stats['invalid_rows']==1

def test_canonical_final_is_label_source_and_conflicts_fail_closed(tmp_path:Path):
    ledger=tmp_path/'execution.jsonl'
    final={
        'event_type':'FINAL','strategy':'CRYPTO_SETTLEMENT_ENGINE','paper_only':True,
        'model_sha':SHA,'market_id':'m1','recorded_ts_ms':200_000,'record_id':'r1',
        'metadata':{'settlement_payouts':{'yes':1.0,'no':0.0}},
    }
    ledger.write_text(json.dumps(final)+'\n')
    labels=settlement_labels_from_ledger(ledger,expected_model_sha=SHA)
    assert labels['m1']['token_payouts']=={'yes':1.0,'no':0.0}
    assert labels['m1']['observed_ns']==200_000_000_000
    conflicting={**final,'record_id':'r2','recorded_ts_ms':200_001,
                 'metadata':{'settlement_payouts':{'yes':0.0,'no':1.0}}}
    ledger.write_text(json.dumps(final)+'\n'+json.dumps(conflicting)+'\n')
    try: settlement_labels_from_ledger(ledger,expected_model_sha=SHA)
    except ValueError: pass
    else: raise AssertionError('conflicting FINAL accepted')
