from __future__ import annotations
import gzip
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import v7_native_repricing_dataset as m


def test_repricing_diagnostics_include_informative_rejections():
    assert {1, 4, 8, 13, 14, 15, 16, 17, 18} <= m.ORIGIN_REASONS


def row(kind=2, h=None, pair=(4000,4200,5800,6000), observed=1_000_000_000):
    yb, ya, nb, na = pair
    return {'schema':'polymarket_v7_native_observation_v1','paper_only':True,'execution_authority':False,
      'run_id':'r','market_id':'m','code_sha':'a'*40,'asset':'XRP','horizon':'M5','kind':kind,
      'reason':15,'repricing_origin_signal_version':7,'repricing_horizon_ms':h,
      'repricing_pair_valid':True,'yes_bid_e4':yb,'yes_ask_e4':ya,'no_bid_e4':nb,'no_ask_e4':na,
      'tick_e4':100,'decision_monotonic_ns':1_000_000_000,'observed_monotonic_ns':observed,
      'decision_wall_ns':2_000_000_000,'binance_return_100ms_bp':1.2,
      'confirmation_return_100ms_bp':.2,'confirmation_venue':'COINBASE','signal_age_ns':10_000_000,
      'tte_ns':100_000_000_000}


def rows():
    return [row()] + [row(6,h,(4100,4300,5700,5900),1_000_000_000+h*1_000_000)
                     for h in (100,250,500,1000)]


def capture(path: Path, data=None, capture_id='c1'):
    data = rows() if data is None else data
    for i, r in enumerate(data, 1):
        r.update(capture_id=capture_id, server_id='server1', sequence=i, connection_epoch=1,
                 capture_semantics_version=2, capture_mode='DECISION_WINDOWS',
                 close_monotonic_ns=200_000_000_000, paper_terms_sha256='b'*64)
    write(path, data)
    return data


def write(path: Path, data):
    text = ''.join(json.dumps(r) + '\n' for r in data)
    path.write_text(text)
    first = data[0]
    closed = {k:first[k] for k in ('run_id','server_id','market_id','capture_id','code_sha')}
    closed.update(schema='polymarket_v7_native_capture_closed_v1',closed=True,healthy=True,
                  bytes=len(text.encode()), last_sequence=len(data),
                  watermark_monotonic_ns=max(r['observed_monotonic_ns'] for r in data))
    Path(str(path)+'.closed.json').write_text(json.dumps(closed))


def run_cli(source, output, summary, *extra):
    return subprocess.run([sys.executable,str(ROOT/'scripts/v7_native_repricing_dataset.py'),
        '--input',str(source),'--output',str(output),'--summary',str(summary),*extra],
        capture_output=True,text=True,timeout=15)


# The three original regressions are retained for explicit diagnostic callers.
def test_complete_pair_builds_causal_delta(tmp_path):
    p=tmp_path/'x.jsonl';p.write_text(''.join(json.dumps(x)+'\n' for x in rows()))
    out,s=m.build([p]);assert len(out)==4 and s['censored_horizons']==2
    assert {x['repricing_horizon_ms'] for x in out}=={100,250,500,1000}
    assert all(x['delta_logit']>0 for x in out)
    assert not any(x['eligible_for_executable_training'] for x in out)


def test_missing_or_broken_pair_is_censored_not_zero(tmp_path):
    p=tmp_path/'x.jsonl';bad=row(6,100);bad['repricing_pair_valid']=False
    p.write_text(json.dumps(row())+'\n'+json.dumps(bad)+'\n')
    out,s=m.build([p]);assert out==[] and s['censored_horizons']==6


def test_conflicting_identity_fails(tmp_path):
    p=tmp_path/'x.jsonl';a=row();b=row();b['yes_bid_e4']=3900
    p.write_text(json.dumps(a)+'\n'+json.dumps(b)+'\n')
    with pytest.raises(ValueError,match='conflicting'):m.build([p])


def test_restarts_with_repeated_signal_numbers_never_collide(tmp_path):
    a,b=tmp_path/'a.jsonl',tmp_path/'b.jsonl'
    capture(a,capture_id='before-restart');data=capture(b,capture_id='after-restart')
    data[0].update(yes_bid_e4=3900,yes_ask_e4=4100,no_bid_e4=5900,no_ask_e4=6100)
    write(b,data)
    out,s=m.build([a,b],require_closed=True)
    assert len(out)==8 and s['origins']==2
    assert {r['capture_id'] for r in out}=={'before-restart','after-restart'}
    assert all(r['producer_closed'] for r in out)


def test_labels_from_other_capture_never_complete_an_origin(tmp_path):
    a,b=tmp_path/'a.jsonl',tmp_path/'b.jsonl'
    capture(a,[row()],capture_id='origin');capture(b,rows()[1:],capture_id='other')
    out,s=m.build([a,b],require_closed=True)
    assert not out and s['censored_horizons']==6


def test_legacy_files_without_capture_identity_never_join(tmp_path):
    a,b=tmp_path/'a.jsonl',tmp_path/'b.jsonl'
    a.write_text(json.dumps(row())+'\n')
    b.write_text(''.join(json.dumps(x)+'\n' for x in rows()[1:]))
    out,s=m.build([a,b]);assert not out and s['censored_horizons']==6


@pytest.mark.parametrize('field,value',[('code_sha','c'*40),('server_id','other'),('market_id','other')])
def test_other_identity_does_not_join(tmp_path,field,value):
    a,b=tmp_path/'a.jsonl',tmp_path/'b.jsonl'
    capture(a,[row()]);data=capture(b,rows()[1:])
    for r in data:r[field]=value
    write(b,data)
    assert not m.build([a,b],require_closed=True)[0]


@pytest.mark.parametrize('field,value',[('connection_epoch',2),('tick_e4',200),
    ('decision_monotonic_ns',999_000_000),('asset','BTC'),('paper_terms_sha256','c'*64)])
def test_matching_capture_but_wrong_context_is_censored(tmp_path,field,value):
    p=tmp_path/'x.jsonl';data=capture(p)
    for r in data[1:]:r[field]=value
    write(p,data)
    out,s=m.build([p],require_closed=True);assert not out and s['censored_horizons']==6


def test_future_external_feature_is_never_a_predictor(tmp_path):
    p=tmp_path/'x.jsonl';data=capture(p)
    data[0]['external_features']={'input_receive_ns':1_000_000_001,'return_1s':1}
    write(p,data)
    out,s=m.build([p],require_closed=True)
    assert not out and s['excluded']['INVALID_ORIGIN_CLOCK_OR_FEATURE_CUT']==6


def test_recorded_gap_censors_even_when_endpoint_pair_is_valid(tmp_path):
    p=tmp_path/'x.jsonl';data=rows()
    data.insert(1,row(kind=5,observed=1_050_000_000));capture(p,data)
    out,s=m.build([p],require_closed=True)
    assert not out and s['excluded']['LABEL_CONTEXT_CLOCK_OR_CONTINUITY']==4


def test_quiet_valid_pair_remains_a_legitimate_zero_delta(tmp_path):
    p=tmp_path/'x.jsonl';data=rows()
    for r in data[1:]:r.update({k:data[0][k] for k in ('yes_bid_e4','yes_ask_e4','no_bid_e4','no_ask_e4')})
    capture(p,data)
    out,_=m.build([p],require_closed=True)
    assert len(out)==4 and all(r['delta_probability']==0 for r in out)


def test_no_labels_after_contract_close(tmp_path):
    p=tmp_path/'x.jsonl';data=capture(p)
    for r in data:r['close_monotonic_ns']=1_250_000_000
    write(p,data)
    out,s=m.build([p],require_closed=True)
    assert len(out)==1 and out[0]['repricing_horizon_ms']==100 and s['censored_horizons']==5


def test_early_label_is_censored(tmp_path):
    p=tmp_path/'x.jsonl';data=capture(p);data[1]['observed_monotonic_ns']=1_099_000_000
    write(p,data)
    out,s=m.build([p],require_closed=True);assert len(out)==3 and s['censored_horizons']==3


@pytest.mark.parametrize('changes',[{'healthy':False},{'closed':False},{'bytes':0},
    {'last_sequence':9},{'capture_id':'wrong'},{'watermark_monotonic_ns':1}])
def test_bad_closure_is_rejected(tmp_path,changes):
    p=tmp_path/'x.jsonl';capture(p);side=Path(str(p)+'.closed.json')
    meta=json.loads(side.read_text());meta.update(changes);side.write_text(json.dumps(meta))
    with pytest.raises(ValueError):m.build([p],require_closed=True)


def test_sequence_gap_rejected(tmp_path):
    p=tmp_path/'x.jsonl';data=capture(p);data[2]['sequence']=8;write(p,data)
    with pytest.raises(ValueError,match='sequence'):m.build([p],require_closed=True)


def test_partial_tail_not_silently_dropped(tmp_path):
    p=tmp_path/'x.jsonl';p.write_text(json.dumps(row()))
    with pytest.raises(ValueError,match='incomplete'):m.build([p])


@pytest.mark.parametrize('text',['{"kind":2,"kind":6}\n','{"value":NaN}\n','[]\n'])
def test_corrupt_json_rejected(tmp_path,text):
    p=tmp_path/'x.jsonl';p.write_text(text)
    with pytest.raises(ValueError):m.build([p])


def test_duplicate_inputs_and_symlink_rejected(tmp_path):
    p=tmp_path/'x.jsonl';capture(p)
    with pytest.raises(ValueError,match='duplicate'):m.build([p,p])
    q=tmp_path/'copy.jsonl';q.write_bytes(p.read_bytes())
    with pytest.raises(ValueError,match='duplicate'):m.build([p,q])
    link=tmp_path/'link.jsonl';link.symlink_to(p)
    with pytest.raises(ValueError,match='unsafe'):m.build([link])


def test_gzip_uses_same_decoded_hash_and_producer_closure(tmp_path):
    p=tmp_path/'x.jsonl';capture(p)
    gz=tmp_path/'x.jsonl.gz';gz.write_bytes(gzip.compress(p.read_bytes()))
    a,sa=m.build([p],require_closed=True);b,sb=m.build([gz],require_closed=True)
    assert a==b and sa['sources'][0]['decoded_sha256']==sb['sources'][0]['decoded_sha256']


def test_source_limits_enforced(tmp_path,monkeypatch):
    p=tmp_path/'x.jsonl';capture(p);monkeypatch.setattr(m,'MAX_SOURCE_BYTES',20)
    with pytest.raises(ValueError,match='size limit'):m.build([p])


def test_cli_requires_closure_and_never_overwrites(tmp_path):
    p,out,summary=tmp_path/'x.jsonl',tmp_path/'out.jsonl',tmp_path/'summary.json'
    p.write_text(''.join(json.dumps(r)+'\n' for r in rows()))
    assert run_cli(p,out,summary).returncode==2
    assert not out.exists()
    result=run_cli(p,out,summary,'--allow-unsealed-diagnostics')
    assert result.returncode==0,result.stderr
    first=out.read_bytes()
    assert run_cli(p,out,summary,'--allow-unsealed-diagnostics').returncode==2
    assert out.read_bytes()==first
    assert json.loads(summary.read_text())['executable_pnl_claim'] is False


def test_closed_cli_success_contains_source_and_output_hashes(tmp_path):
    p,out,summary=tmp_path/'x.jsonl',tmp_path/'out.jsonl',tmp_path/'summary.json';capture(p)
    result=run_cli(p,out,summary);assert result.returncode==0,result.stderr
    meta=json.loads(summary.read_text())
    import hashlib
    assert meta['output_sha256']==hashlib.sha256(out.read_bytes()).hexdigest()
    assert meta['sources'][0]['producer_closed'] is True
    assert meta['require_closed'] is True


@pytest.mark.parametrize('field,value',[('schema','wrong'),('paper_only',False),('execution_authority',True)])
def test_invalid_record_cannot_hide_inside_healthy_closed_capture(tmp_path,field,value):
    p=tmp_path/'x.jsonl';data=capture(p);data[1][field]=value;write(p,data)
    with pytest.raises(ValueError,match='certified capture'):m.build([p],require_closed=True)


def test_observation_clock_cannot_go_backwards(tmp_path):
    p=tmp_path/'x.jsonl';data=capture(p);data[2]['observed_monotonic_ns']=1_050_000_000;write(p,data)
    with pytest.raises(ValueError,match='backwards'):m.build([p],require_closed=True)


def test_two_sealed_files_cannot_claim_same_capture(tmp_path):
    a,b=tmp_path/'a.jsonl',tmp_path/'b.jsonl'
    capture(a,[row()]);capture(b,rows()[1:])
    with pytest.raises(ValueError,match='duplicate closed capture'):m.build([a,b],require_closed=True)


def test_sub_100ms_labels_use_exact_receive_clock_and_preserve_legacy_labels(tmp_path):
    p=tmp_path/'early.jsonl'
    records=[row()]+[row(6,h,(4100,4300,5700,5900),1_000_000_000+h*1_000_000)
                     for h in (25,50,100,250,500,1000)]
    capture(p,records)
    output,status=m.build([p],require_closed=True)
    assert {r['repricing_horizon_ms'] for r in output}=={25,50,100,250,500,1000}
    assert status['censored_horizons']==0
