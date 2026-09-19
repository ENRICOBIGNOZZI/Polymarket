from __future__ import annotations
import json, os, sys, tempfile, time, unittest
from unittest import mock
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_evidence_store import canonical, digest, immutable
import v7_windowed_evidence_retention as retention
from v7_windowed_evidence_retention import run

def revision(store, source_id, family, path, captured_ns, objsha):
    value={'schema':'polymarket_v7_permanent_source_revision_v1','paper_only':True,
      'authenticated_execution':False,'real_order_submission':False,
      'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','source_id':source_id,
      'partition':'run:x','relative_path':path.name,'original_path_at_capture':str(path),
      'captured_ns':captured_ns,'capture_implementation_sha256':'b'*64,
      'source_bytes':10,'source_physical_bytes':10,'capture_complete':True,
      'source_compression':'none','stat':[1,2,10,captured_ns],
      'previous_revision':None,'supersedes_without_destroying':None,
      'chunks':[{'sha256':objsha,'bytes':10,'compressed_bytes':10,
                 'object':f'objects/{objsha[:2]}/{objsha}.gz','offset':0}],
      'contract':{'source_family':family},'tail_bytes':0,'tail_sha256':digest(b'')}
    payload=canonical(value);sha=digest(payload)
    immutable(store/'revisions'/sha[:2]/(sha+'.json'),payload)
    return sha

class WindowedRetentionTest(unittest.TestCase):
    def test_old_windowed_raw_is_retired_but_ledger_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store'
            old=time.time_ns()-8*3600*10**9
            raw=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/x.segment-1000000.jsonl'
            raw.parent.mkdir(parents=True);raw.write_text('{}\n');os.utime(raw,ns=(old,old))
            obj='c'*64;op=store/'objects'/obj[:2]/(obj+'.gz');op.parent.mkdir(parents=True);op.write_bytes(b'x')
            revision(store,'raw-source','pm_causal_book',raw,old,obj)
            ledger=runs/'paper_v7_live/ledger/execution.jsonl';ledger.parent.mkdir(parents=True);ledger.write_text('ledger')
            lobj='d'*64;lop=store/'objects'/lobj[:2]/(lobj+'.gz');lop.parent.mkdir(parents=True);lop.write_bytes(b'x')
            revision(store,'ledger-source','canonical_ledger',ledger,old,lobj)
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(raw.exists());self.assertFalse(op.exists());self.assertTrue(lop.exists())
            receipt=json.loads((store/'retired_sources/raw-source.json').read_text())
            self.assertFalse(receipt['raw_detail_available']);self.assertTrue(ledger.exists())
            self.assertEqual(out['retired_sources'],1)

    def test_recent_windowed_raw_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store'
            recent=time.time_ns()-3600*10**9
            raw=runs/'paper_v7_live/research/repricing_book/book_observations/x.segment-1000000.jsonl'
            raw.parent.mkdir(parents=True);raw.write_text('{}\n');os.utime(raw,ns=(recent,recent))
            obj='e'*64;op=store/'objects'/obj[:2]/(obj+'.gz');op.parent.mkdir(parents=True);op.write_bytes(b'x')
            revision(store,'raw-source','pm_causal_book',raw,recent,obj)
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertTrue(raw.exists());self.assertTrue(op.exists());self.assertEqual(out['retired_sources'],0)

    def test_old_manifest_only_windowed_pack_is_retired(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store'
            alias=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/x.jsonl'
            pack_payload=b'old-pack';sha=hashlib.sha256(pack_payload).hexdigest()
            pack=store/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True);pack.write_bytes(pack_payload);os.chmod(pack,0o400)
            value={'schema':'polymarket_v7_lossless_shared_pack_v1','pack_sha256':sha,'pack_bytes':len(pack_payload),
                'source_aliases':[str(alias)],'source_bytes_sha256_verified':True,'created_ns':time.time_ns()-8*3600*10**9,
                'source_original_stat':[1,2,len(pack_payload),time.time_ns()-8*3600*10**9]}
            raw=json.dumps(value).encode();man=store/'pack_manifests'/(hashlib.sha256(raw).hexdigest()+'.json');man.parent.mkdir(parents=True);man.write_bytes(raw)
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(pack.exists());self.assertFalse(man.exists())
            self.assertTrue((store/'windowed_pack_tombstones'/(sha+'.json')).exists())
            self.assertEqual(out['removed_packs'],1)

    def test_manifest_pack_with_protected_alias_survives(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store'
            raw_alias=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/x.jsonl'
            protected_alias=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'ledger/execution.jsonl'
            payload=b'mixed-pack';sha=hashlib.sha256(payload).hexdigest();pack=store/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True);pack.write_bytes(payload);os.chmod(pack,0o400)
            old=time.time_ns()-8*3600*10**9
            value={'schema':'polymarket_v7_lossless_shared_pack_v1','pack_sha256':sha,'pack_bytes':len(payload),
                'source_aliases':[str(raw_alias),str(protected_alias)],'source_bytes_sha256_verified':True,'created_ns':old,'source_original_stat':[1,2,len(payload),old]}
            raw=json.dumps(value).encode();man=store/'pack_manifests'/(hashlib.sha256(raw).hexdigest()+'.json');man.parent.mkdir(parents=True);man.write_bytes(raw)
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertTrue(pack.exists());self.assertTrue(man.exists());self.assertEqual(out['removed_packs'],0)


    def test_checked_in_policy_matches_window_worker_contract(self):
        cfg=json.loads((ROOT/'config/v7_data_retention.json').read_text())
        policy=cfg['rolling_window']
        from v7_windowed_evidence_retention import POLICY, WINDOWED
        self.assertTrue(policy['enabled']);self.assertEqual(policy['authorization'],POLICY)
        self.assertEqual(policy['raw_detail_seconds'],28800)
        self.assertEqual(set(policy['windowed_source_families']),WINDOWED)
        self.assertEqual(policy['target_managed_bytes'],50_000_000_000)
        self.assertEqual(policy['trigger_managed_bytes'],54_000_000_000)
        self.assertEqual(policy['maximum_managed_bytes'],60_000_000_000)
        self.assertFalse(cfg['aggregate_retention']['hard_filesystem_quota'])


    def test_existing_tombstone_resumes_cleanup_without_collision(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store'
            alias=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/x.jsonl'
            payload=b'large-old-pack';sha=hashlib.sha256(payload).hexdigest()
            pack=store/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True);pack.write_bytes(payload);os.chmod(pack,0o400)
            old=time.time_ns()-8*3600*10**9
            value={'schema':'polymarket_v7_lossless_shared_pack_v1','pack_sha256':sha,'pack_bytes':len(payload),
                'source_aliases':[str(alias)],'source_bytes_sha256_verified':True,'created_ns':old,'source_original_stat':[1,2,len(payload),old]}
            raw=json.dumps(value).encode();man=store/'pack_manifests'/(hashlib.sha256(raw).hexdigest()+'.json');man.parent.mkdir(parents=True);man.write_bytes(raw)
            tomb={'schema':'polymarket_v7_windowed_pack_tombstone_v1','paper_only':True,'authenticated_execution':False,
                  'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
                  'policy':'USER_AUTHORIZED_HFT_ROLLING_RAW_WINDOW_20260916','raw_detail_available':False,
                  'pack_sha256':sha,'source_aliases':[str(alias)],'source_families':['pm_causal_book'],
                  'object_sha256s':[],'manifest_sha256s':[man.stem],'retired_at_ns':old,'cutoff_ns':old-1}
            target=store/'windowed_pack_tombstones'/(sha+'.json');immutable(target,canonical(tomb))
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(pack.exists());self.assertFalse(man.exists());self.assertTrue(target.exists())
            self.assertEqual(out['removed_packs'],1)

    def test_existing_tombstone_records_new_manifest_before_resume(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store';old=time.time_ns()-8*3600*10**9
            alias=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'external_fair/tapes/x.bin'
            payload=b'pack-with-regenerated-manifest';sha=hashlib.sha256(payload).hexdigest()
            pack=store/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True);pack.write_bytes(payload);os.chmod(pack,0o400)
            value={'schema':'polymarket_v7_lossless_shared_pack_v1','pack_sha256':sha,'pack_bytes':len(payload),
                   'source_aliases':[str(alias)],'source_bytes_sha256_verified':True,'created_ns':old,'source_original_stat':[1,2,len(payload),old]}
            raw=json.dumps(value).encode();man=store/'pack_manifests'/(hashlib.sha256(raw).hexdigest()+'.json');man.parent.mkdir(parents=True);man.write_bytes(raw)
            tomb={'schema':'polymarket_v7_windowed_pack_tombstone_v1','paper_only':True,'authenticated_execution':False,
                  'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
                  'policy':'USER_AUTHORIZED_HFT_ROLLING_RAW_WINDOW_20260916','raw_detail_available':False,
                  'pack_sha256':sha,'source_aliases':[str(alias)],'source_families':['external_normalized'],
                  'object_sha256s':[],'manifest_sha256s':['f'*64],'retired_at_ns':old,'cutoff_ns':old-1}
            parent=store/'windowed_pack_tombstones'/(sha+'.json');immutable(parent,canonical(tomb))
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(pack.exists());self.assertFalse(man.exists());self.assertTrue(parent.exists())
            receipts=list((store/'windowed_pack_resume_receipts').glob('*/*.json'))
            self.assertEqual(len(receipts),1)
            receipt=json.loads(receipts[0].read_text())
            self.assertEqual(receipt['added_manifest_sha256s'],[man.stem])
            self.assertEqual(receipt['parent_tombstone_sha256'],hashlib.sha256(canonical(tomb)).hexdigest())
            self.assertEqual(out['removed_packs'],1)

    def test_existing_tombstone_appends_new_alias_provenance(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store';old=time.time_ns()-8*3600*10**9
            a1=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/a.jsonl'
            a2=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/b.jsonl'
            payload=b'pack';sha=hashlib.sha256(payload).hexdigest();pack=store/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True);pack.write_bytes(payload);os.chmod(pack,0o400)
            value={'schema':'polymarket_v7_lossless_shared_pack_v1','pack_sha256':sha,'pack_bytes':len(payload),
                   'source_aliases':[str(a1),str(a2)],'source_bytes_sha256_verified':True,'created_ns':old,'source_original_stat':[1,2,len(payload),old]}
            raw=json.dumps(value).encode();man=store/'pack_manifests'/(hashlib.sha256(raw).hexdigest()+'.json');man.parent.mkdir(parents=True);man.write_bytes(raw)
            tomb={'schema':'polymarket_v7_windowed_pack_tombstone_v1','paper_only':True,'authenticated_execution':False,
                  'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
                  'policy':'USER_AUTHORIZED_HFT_ROLLING_RAW_WINDOW_20260916','raw_detail_available':False,
                  'pack_sha256':sha,'source_aliases':[str(a1)],'source_families':['pm_causal_book'],
                  'object_sha256s':[],'manifest_sha256s':[man.stem],'retired_at_ns':old,'cutoff_ns':old-1}
            immutable(store/'windowed_pack_tombstones'/(sha+'.json'),canonical(tomb))
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(pack.exists());self.assertFalse(man.exists())
            receipts=list((store/'windowed_pack_resume_receipts').glob('*/*.json'))
            self.assertEqual(len(receipts),1)
            receipt=json.loads(receipts[0].read_text())
            self.assertEqual(receipt['added_source_aliases'],[str(a2)])
            self.assertEqual(out['removed_packs'],1)


    def test_pack_cleanup_retires_verified_hardlink_aliases(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store';old=time.time_ns()-8*3600*10**9
            alias=runs/'paper_v7_durable/external_cancel/workspaces/1/normalized_events/x.segment-000000.bin'
            alias.parent.mkdir(parents=True);payload=b'hardlinked-windowed-payload';alias.write_bytes(payload);os.utime(alias,ns=(old,old))
            sha=hashlib.sha256(payload).hexdigest();pack=store/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True);os.link(alias,pack);os.chmod(pack,0o400)
            value={'schema':'polymarket_v7_lossless_shared_pack_v1','pack_sha256':sha,'pack_bytes':len(payload),
                   'source_aliases':[str(alias)],'source_bytes_sha256_verified':True,'created_ns':old,'source_original_stat':[alias.stat().st_dev,alias.stat().st_ino,len(payload),old]}
            raw=json.dumps(value).encode();man=store/'pack_manifests'/(hashlib.sha256(raw).hexdigest()+'.json');man.parent.mkdir(parents=True);man.write_bytes(raw)
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(alias.exists());self.assertFalse(pack.exists());self.assertFalse(man.exists())
            self.assertEqual(out['removed_source_aliases'],1)
            self.assertTrue(list((store/'windowed_alias_retirement_receipts').glob('*/*.json')))

    def test_tombstone_only_alias_recovery_hashes_before_retirement(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store';old=time.time_ns()-8*3600*10**9
            alias=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/recovered.jsonl'
            alias.parent.mkdir(parents=True);payload=b'closed-old-alias';alias.write_bytes(payload);os.utime(alias,ns=(old,old))
            sha=hashlib.sha256(payload).hexdigest()
            tomb={'schema':'polymarket_v7_windowed_pack_tombstone_v1','paper_only':True,'authenticated_execution':False,'real_order_submission':False,
                  'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','policy':'USER_AUTHORIZED_HFT_ROLLING_RAW_WINDOW_20260916','raw_detail_available':False,
                  'pack_sha256':sha,'source_aliases':[str(alias)],'source_families':['pm_causal_book'],'object_sha256s':[],'manifest_sha256s':[],
                  'retired_at_ns':old,'cutoff_ns':old-1}
            immutable(store/'windowed_pack_tombstones'/(sha+'.json'),canonical(tomb))
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(alias.exists());self.assertEqual(out['removed_source_aliases'],1)
            receipt=json.loads(next((store/'windowed_alias_retirement_receipts').glob('*/*.json')).read_text())
            self.assertEqual(receipt['alias']['verified_by'],'FULL_SHA256')

    def test_tombstone_only_alias_hash_mismatch_is_preserved(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs';store=runs/'paper_v7_durable/permanent_evidence/store';old=time.time_ns()-8*3600*10**9
            alias=runs/'paper_v7_archives'/('cutover-'+'a'*40+'-1-1')/'research/repricing_book/book_observations/recovered.jsonl'
            alias.parent.mkdir(parents=True);alias.write_bytes(b'changed');os.utime(alias,ns=(old,old))
            sha=hashlib.sha256(b'original').hexdigest()
            tomb={'schema':'polymarket_v7_windowed_pack_tombstone_v1','paper_only':True,'authenticated_execution':False,'real_order_submission':False,
                  'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','policy':'USER_AUTHORIZED_HFT_ROLLING_RAW_WINDOW_20260916','raw_detail_available':False,
                  'pack_sha256':sha,'source_aliases':[str(alias)],'source_families':['pm_causal_book'],'object_sha256s':[],'manifest_sha256s':[],
                  'retired_at_ns':old,'cutoff_ns':old-1}
            immutable(store/'windowed_pack_tombstones'/(sha+'.json'),canonical(tomb))
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertTrue(alias.exists());self.assertEqual(out['removed_source_aliases'],0);self.assertEqual(out['alias_integrity_skips'],1)


    def test_unindexed_old_pm_book_gets_hash_receipt_before_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs'; store=runs/'paper_v7_durable/permanent_evidence/store'; store.mkdir(parents=True)
            old=time.time_ns()-8*3600*10**9
            source=runs/'paper_v7_live/micro_maker/book_observations/session.segment-1000000.jsonl'
            source.parent.mkdir(parents=True); payload=b'{"book":1}\n'*100; source.write_bytes(payload); os.utime(source,ns=(old,old))
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertFalse(source.exists())
            self.assertEqual(out['removed_unindexed_source_files'],1)
            receipts=list((store/'unindexed_windowed_source_tombstones').glob('*/*.json'))
            self.assertEqual(len(receipts),1)
            receipt=json.loads(receipts[0].read_text())
            self.assertEqual(Path(receipt['source']['path']).resolve(),source.resolve())
            self.assertEqual(receipt['source']['source_family'],'pm_causal_book')
            import hashlib
            self.assertEqual(receipt['source']['sha256'],hashlib.sha256(payload).hexdigest())
            self.assertFalse(receipt['raw_detail_available'])

    def test_recent_unindexed_pm_book_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs'; (runs/'paper_v7_durable/permanent_evidence/store').mkdir(parents=True)
            source=runs/'paper_v7_live/research/repricing_book/book_observations/session.segment-1000000.jsonl'
            source.parent.mkdir(parents=True); source.write_text('{}\n'); recent=time.time_ns()-3600*10**9; os.utime(source,ns=(recent,recent))
            out=run(runs,raw_detail_seconds=6*3600,maximum_seconds=30)
            self.assertTrue(source.exists()); self.assertEqual(out['unindexed_source_candidates'],0)

    def test_indexed_path_is_not_considered_unindexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs'; old=time.time_ns()-8*3600*10**9
            source=runs/'paper_v7_live/micro_maker/book_observations/session.segment-1000000.jsonl'
            source.parent.mkdir(parents=True); source.write_text('{}\n'); os.utime(source,ns=(old,old))
            rows=retention._unindexed_pm_book_segments(runs,indexed_paths={str(source)},cutoff_ns=time.time_ns()-6*3600*10**9)
            self.assertEqual(rows,[]); self.assertTrue(source.exists())

    def test_unindexed_hash_race_preserves_source_and_writes_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs=Path(tmp)/'runs'; store=runs/'paper_v7_durable/permanent_evidence/store'; store.mkdir(parents=True)
            source=runs/'paper_v7_live/micro_maker/book_observations/session.segment-1000000.jsonl'
            source.parent.mkdir(parents=True); source.write_bytes(b'first')
            original=retention._sha256_file
            def mutate(path):
                digest=original(path); path.write_bytes(b'second-value'); return digest
            with mock.patch.object(retention,'_sha256_file',side_effect=mutate):
                reclaimed,status=retention._retire_unindexed_windowed_path(source,store=store,family='pm_causal_book',raw_detail_seconds=21600,dry_run=False)
            self.assertEqual(reclaimed,0); self.assertEqual(status,'SOURCE_CHANGED_DURING_HASH'); self.assertTrue(source.exists())
            self.assertFalse((store/'unindexed_windowed_source_tombstones').exists())


if __name__=='__main__':unittest.main()
