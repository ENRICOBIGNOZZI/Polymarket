import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_aggregate_retention import HEADER,RAW,RECORD,EVENT,summarize,retire,eligible,resume_expiries
from v7_evidence_store import EvidenceStore

NOW=1_800_000_000
WALL=(NOW-7200)*10**9

def tape(raw=False,tail=b''):
    h=HEADER.pack(b'PMV7RAW!' if raw else b'PMV7TAPE',3,0 if raw else 544,WALL,b'a'*40,b'run',b'session',b'binance-spot')
    rows=[]
    for seq,price in [(1,100.),(3,110.)]:
        if raw:
            p=b'{"price":100}'
            rows.append(RAW.pack(seq,1,seq,WALL+seq,1,len(p))+p)
        else:
            event=EVENT.pack(7,seq,1,1,2,0,0,seq,WALL+seq,0,0,0,0,price,2,0,0,0,0,1,0,0,0,1)
            rows.append(RECORD.pack(seq,seq,7,2,0,len(event))+event+bytes(512-len(event)))
    return h+b''.join(rows)+tail

class AggregateRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runs=Path(self.tmp.name)/'runs'
        self.folder=self.runs/'paper_v7_archives'/('cutover-'+'a'*40+'-123-456')/'external_fair'
        self.store=EvidenceStore(self.runs/'paper_v7_durable/permanent_evidence/store');self.addCleanup(self.store.close)
    def source(self,raw=False,tail=b''):
        p=self.folder/('raw' if raw else 'normalized_events')/'binance-spot.123.bin.gz'
        p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(gzip.compress(tape(raw,tail),mtime=0));os.utime(p,(NOW-7200,NOW-7200));return p
    def capture(self,p,**kw):
        return self.store.capture(p,partition='run:a',relative=str(p.relative_to(self.folder.parent)),contract={'source_family':'external_normalized'},**kw)
    def test_numeric_market_statistics_conserve_records_and_native_flow(self):
        d=summarize(self.source(),now_ns=NOW*10**9)
        self.assertEqual(d['record_count'],2);b=d['bins'][0]
        self.assertEqual(b['sequence_gaps'],1);self.assertEqual(b['metrics']['trade_price'][:4],[2,210.,100.,110.])
        self.assertEqual(b['metrics']['signed_trade_size'][1],4)
        self.assertEqual(b['metrics']['trade_price_times_native_size'][1],420)
        self.assertEqual(b['flags']['missing_exchange_clock'],2)
    def test_raw_scope_does_not_claim_payload_market_coverage(self):
        d=summarize(self.source(True),now_ns=NOW*10**9)
        self.assertEqual(d['scope'],'RAW_TRANSPORT_COUNTS_ONLY');self.assertEqual(d['bins'][0]['metrics'],{})
        self.assertEqual(d['record_count'],2)
    def test_raw_queue_reordering_is_preserved_as_quality_counts(self):
        p=self.source(True);raw=bytearray(tape(True))
        second=HEADER.size+RAW.size+len(b'{"price":100}')
        raw[second:second+8]=(1).to_bytes(8,'little')
        p.write_bytes(gzip.compress(raw));d=summarize(p,now_ns=NOW*10**9)
        self.assertEqual(d['record_count'],2);self.assertEqual(d['bins'][0]['sequence_regressions'],1)
    def test_recompression_manifest_can_include_pack_itself(self):
        from v7_lossless_data_compaction import file_hash
        p=self.source();self.capture(p);sha=file_hash(p)
        pack=self.store.root/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True);os.link(p,pack)
        folder=self.store.root/'pack_manifests';folder.mkdir()
        (folder/'test.json').write_text(json.dumps({'pack_sha256':sha,'source_aliases':[str(p),str(pack)]}))
        retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        self.assertFalse(pack.exists());self.assertFalse(p.exists())
    def test_sealed_partial_record_prevents_deletion(self):
        p=self.source(tail=b'x')
        with self.assertRaisesRegex(ValueError,'partial'):retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        self.assertTrue(p.exists());self.assertFalse((self.store.root/'expiry_receipts').exists())
    def test_expiry_is_explicit_and_index_rebuild_keeps_provenance(self):
        p=self.source();ref=self.capture(p)
        d=retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        self.assertFalse(p.exists());self.assertEqual(d['record_count'],2)
        with self.assertRaisesRegex(ValueError,'RAW_DETAIL_EXPIRED_AFTER_AGGREGATION'):list(self.store.bytes(ref['revision']))
        self.assertEqual(self.store.rebuild_index()['sources'],1)
        a=json.loads(gzip.decompress((self.store.root/d['aggregate']).read_bytes()))
        self.assertEqual(a['record_count'],2)
    def test_protected_other_source_keeps_shared_object(self):
        p=self.source();self.capture(p)
        q=self.runs/'ledger/execution.jsonl.gz';q.parent.mkdir();q.write_bytes(p.read_bytes())
        protected=self.store.capture(q,partition='protected',relative='ledger/execution.jsonl.gz',contract={})
        retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        self.assertEqual(b''.join(self.store.bytes(protected['revision'])),tape())
    def test_live_open_tape_and_unknown_hardlink_protected(self):
        p=self.source()
        with self.assertRaisesRegex(ValueError,'open'):retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:False)
        link=self.runs/'uninspected';os.link(p,link)
        with self.assertRaisesRegex(ValueError,'hardlink'):retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        self.assertTrue(p.exists())
    def test_recent_clock_prevents_mtime_bypass(self):
        p=self.source();os.utime(p,(NOW-20000,NOW-20000))
        with self.assertRaisesRegex(ValueError,'recent observations'):retire(p,self.runs,self.store,now=NOW-7000,check_closed=lambda _:True)
        self.assertTrue(p.exists())
    def test_unknown_abi_and_ledger_never_eligible(self):
        p=self.source();b=bytearray(tape());b[8]=2;p.write_bytes(gzip.compress(b));os.utime(p,(NOW-7200,NOW-7200))
        with self.assertRaisesRegex(ValueError,'unsupported tape ABI'):retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        ledger=self.folder.parent/'ledger/execution.jsonl';ledger.parent.mkdir();ledger.write_bytes(b);os.utime(ledger,(NOW-7200,NOW-7200))
        self.assertFalse(eligible(ledger,self.runs,NOW,3600))
    def test_interrupted_unlink_resumes_from_verified_aggregate(self):
        p=self.source();ref=self.capture(p);original=Path.unlink
        def fail_source(path,*args,**kwargs):
            if path==p:raise OSError('simulated power interruption')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_source):
            with self.assertRaisesRegex(OSError,'interruption'):retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        self.assertTrue(p.exists())
        with patch('v7_aggregate_retention.time.time',return_value=NOW):
            self.assertEqual(len(resume_expiries(self.store,self.runs,check_closed=lambda _:True)),1)
        self.assertFalse(p.exists())
        with self.assertRaisesRegex(ValueError,'RAW_DETAIL_EXPIRED'):list(self.store.bytes(ref['revision']))
    def test_corrupt_aggregate_blocks_interruption_recovery(self):
        p=self.source();self.capture(p);original=Path.unlink
        def fail_source(path,*args,**kwargs):
            if path==p:raise OSError('interruption')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_source):
            with self.assertRaises(OSError):retire(p,self.runs,self.store,now=NOW,check_closed=lambda _:True)
        aggregate=next((self.store.root/'aggregates').glob('*/*.gz'));aggregate.write_bytes(gzip.compress(b'{}'))
        with self.assertRaisesRegex(ValueError,'aggregate receipt mismatch'):resume_expiries(self.store,self.runs,check_closed=lambda _:True)
        self.assertTrue(p.exists())
    def test_symlink_parent_not_eligible(self):
        p=self.source();alias=self.runs/'paper_v7_archives'/('cutover-'+'b'*40+'-123-456');alias.symlink_to(self.folder.parent,target_is_directory=True)
        self.assertFalse(eligible(alias/'external_fair/normalized_events'/p.name,self.runs,NOW,3600))

if __name__=='__main__':unittest.main()
