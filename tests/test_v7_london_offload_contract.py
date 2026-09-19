from __future__ import annotations
import json,tempfile,time,hashlib
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'monitoring'))
from v7_london_buffer_retention import run

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def config(): return json.loads((ROOT/'config/v7_london_buffer_retention.json').read_text())

def test_no_verified_offload_means_no_delete():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);p=root/'external_fair/raw/x.segment-000001.bin';p.parent.mkdir(parents=True);p.write_bytes(b'x'*1024)
        v=run(root,config());assert v['state']=='NO_VERIFIED_OFFLOAD';assert p.exists()

def test_only_exact_hash_synced_closed_segment_can_be_deleted():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);p=root/'external_fair/raw/x.segment-000001.bin';p.parent.mkdir(parents=True);p.write_bytes(b'x'*1024)
        old=time.time()-7200;p.touch();import os;os.utime(p,(old,old))
        c=config();c['target_managed_bytes']=1;c['maximum_managed_bytes']=10_000
        receipt=root/'control/research_offload_receipt.json';receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'schema':'polymarket_v7_research_offload_receipt_v1','synced_through_ns':time.time_ns(),'files':[{'path':str(p.relative_to(root)),'size':p.stat().st_size,'sha256':sha(p)}]}))
        v=run(root,c);assert not p.exists();assert v['deleted']

def test_canonical_live_surfaces_are_never_delete():
    c=config(); protected=set(c['never_delete'])
    assert {'ledger/execution.jsonl','trade_tape.csv','micro_maker/book_observations/current.jsonl','research/repricing_book/book_observations/current.jsonl'} <= protected
