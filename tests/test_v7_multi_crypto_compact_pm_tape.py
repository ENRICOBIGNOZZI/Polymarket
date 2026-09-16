#!/usr/bin/env python3
from __future__ import annotations
import json, struct, sys, tempfile
from pathlib import Path
ROOT=Path('/Users/enrico/polymarket-multi-crypto-v7'); sys.path.insert(0,str(ROOT/'scripts'))
from v7_multi_crypto_compact_pm_tape import RECORD, build_timelines, load_manifest, pair_asof, read_records, validate_status
SHA='a'*40

def manifest(tmp: Path) -> Path:
    value={"schema":"polymarket_v7_compact_pm_label_tape_manifest_v1","version":1,
           "record_schema":"polymarket_v7_compact_pm_label_record_v1","record_size":64,
           "byte_order":"little_endian","model_sha":SHA,"observer_session_id":"s1",
           "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
           "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","selection_only":True,
           "tokens":[
             {"instrument_handle":1,"market_handle":1,"market_id":"m","event_id":"e","token_id":"y","outcome":"YES","initial_tick_e4":100},
             {"instrument_handle":2,"market_handle":1,"market_id":"m","event_id":"e","token_id":"n","outcome":"NO","initial_tick_e4":100}]}
    path=tmp/'m.json'; path.write_text(json.dumps(value)); return path

def rec(seq,handle,wall,bid,ask,epoch=1,valid=1,lineage=1,tick=100):
    return RECORD.pack(seq,handle,seq,epoch,wall,wall*1_000_000,bid,ask,tick,valid,lineage,1,0)

def test_reader_and_pair_asof_are_exact_receive_time():
    with tempfile.TemporaryDirectory() as td:
        tmp=Path(td); mp=manifest(tmp); tape=tmp/'t.bin'
        tape.write_bytes(b''.join([
            rec(1,1,1000,4900,5100), rec(2,2,1000,4900,5100),
            rec(3,1,1050,5000,5200), rec(4,2,1060,4700,4900)]))
        m=load_manifest(mp,SHA); rows=read_records([tape],m); tl=build_timelines(rows)
        p0=pair_asof(tl,'m',1000); assert p0 and abs(p0['pm_yes']-.5)<1e-12
        p1=pair_asof(tl,'m',1055); assert p1 and abs(p1['pm_yes']-.505)<1e-12
        p2=pair_asof(tl,'m',1060); assert p2 and p2['state_available_wall_ms']==1060

def test_future_record_is_never_used_by_asof():
    with tempfile.TemporaryDirectory() as td:
        tmp=Path(td); mp=manifest(tmp); tape=tmp/'t.bin'
        tape.write_bytes(rec(1,1,1100,6000,6200)+rec(2,2,1100,3800,4000))
        tl=build_timelines(read_records([tape],load_manifest(mp,SHA)))
        assert pair_asof(tl,'m',1099) is None
        assert pair_asof(tl,'m',1100) is not None

def test_epoch_mismatch_and_invalid_book_are_censored():
    with tempfile.TemporaryDirectory() as td:
        tmp=Path(td); mp=manifest(tmp); tape=tmp/'t.bin'
        tape.write_bytes(rec(1,1,1000,4900,5100,epoch=1)+rec(2,2,1000,4900,5100,epoch=2))
        tl=build_timelines(read_records([tape],load_manifest(mp,SHA))); assert pair_asof(tl,'m',1000) is None
        tape.write_bytes(rec(1,1,1000,4900,5100,valid=0)+rec(2,2,1000,4900,5100))
        tl=build_timelines(read_records([tape],load_manifest(mp,SHA))); assert pair_asof(tl,'m',1000) is None

def test_partial_binary_and_nonmonotone_sequence_fail_closed():
    with tempfile.TemporaryDirectory() as td:
        tmp=Path(td); mp=manifest(tmp); m=load_manifest(mp,SHA); tape=tmp/'t.bin'
        tape.write_bytes(b'x')
        try: read_records([tape],m)
        except ValueError as exc: assert 'partial_record' in str(exc)
        else: raise AssertionError('partial record accepted')
        tape.write_bytes(rec(2,1,1000,4900,5100)+rec(1,2,1001,4900,5100))
        try: read_records([tape],m)
        except ValueError as exc: assert 'nonmonotone_sequence' in str(exc)
        else: raise AssertionError('nonmonotone sequence accepted')

def test_status_requires_complete_no_reconnect_evidence():
    with tempfile.TemporaryDirectory() as td:
        tmp=Path(td); m=load_manifest(manifest(tmp),SHA)
        status={"paper_only":True,"authenticated_execution":False,"real_order_submission":False,
                "model_sha":SHA,"observer_session_id":"s1","evidence_complete":True,
                "compact_label_tape_enabled":True,"compact_label_record_size":64,
                "dropped_events":0,"decoder_failures":0,"reconnects":0,"feed_reconnects":0}
        validate_status(status,m)
        status['reconnects']=1
        try: validate_status(status,m)
        except ValueError as exc: assert 'reconnect' in str(exc)
        else: raise AssertionError('reconnect accepted')

if __name__=='__main__':
    tests=sorted((n,f) for n,f in globals().items() if n.startswith('test_') and callable(f))
    for _,f in tests:f()
    print(f'{len(tests)} function tests passed')
