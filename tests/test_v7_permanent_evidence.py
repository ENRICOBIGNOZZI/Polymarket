from __future__ import annotations
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_evidence_store import EvidenceStore
from v7_evidence_catalog import classify, inventory
from v7_permanent_evidence import collect,sources


class PermanentEvidenceTests(unittest.TestCase):
    def test_data_budget_defers_duplicate_capture_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);live=root/'live';live.mkdir();source=live/'source.jsonl';source.write_text('{"x":1}\n')
            with mock.patch('v7_permanent_evidence.allocated_data_bytes',return_value=40_000_000_000):
                result=collect(live,root/'durable',root/'archives')
            self.assertEqual(result['state'],'ARCHIVE_COPY_DEFERRED_DATA_BUDGET')
            self.assertEqual(source.read_text(),'{"x":1}\n')
            self.assertFalse((root/'durable/permanent_evidence/store').exists())

    def test_permanent_derived_artifacts_never_recursively_capture_themselves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);live=root/'live';live.mkdir();durable=root/'durable'
            path=durable/'permanent_evidence/new_future_dataset/chunk.jsonl';path.parent.mkdir(parents=True);path.write_text('{}\n')
            raw=durable/'external/raw.bin';raw.parent.mkdir();raw.write_bytes(b'raw')
            paths=[item[3] for item in sources(live,durable,root/'archives',None)]
            self.assertIn(raw,paths);self.assertNotIn(path,paths)

    def test_inode_replacement_with_matching_tail_cannot_reuse_changed_prefix(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'source';original=b'a'*100+b'z'*5000;path.write_bytes(original)
            with EvidenceStore(root/'store',chunk_bytes=1024) as store:
                a=store.capture(path,partition='A',relative='source',contract={},append=True)
                new=root/'new';new.write_bytes(b'b'*100+b'z'*5000);os.replace(new,path)
                b=store.capture(path,partition='A',relative='source',contract={},append=True)
                self.assertIsNone(store.revision(b['revision'])['previous_revision'])
                self.assertEqual(b''.join(store.bytes(a['revision'])),original)
                self.assertEqual(b''.join(store.bytes(b['revision'])),path.read_bytes())

    def test_model_a_cutover_model_b_rebuild_both_without_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);live=root/'live';live.mkdir();source=live/'decisions.jsonl'
            a={'model_hash':'a'*64,'feature_schema_version':'v1','replay_key':'same-opportunity','p':.4}
            b={'model_hash':'b'*64,'feature_schema_version':'v2','replay_key':'same-opportunity','p':.6}
            source.write_text(json.dumps(a)+'\n')
            with EvidenceStore(root/'permanent',chunk_bytes=17) as store:
                ra=store.capture(source,partition='run-A',relative='decisions.jsonl',contract={'semantics':'v1'},append=True)
                live.rename(root/'archive-A');live.mkdir();source=live/'decisions.jsonl';source.write_text(json.dumps(b)+'\n')
                rb=store.capture(source,partition='run-B',relative='decisions.jsonl',contract={'semantics':'v2'},append=True)
                self.assertEqual(list(store.json_rows(ra['revision'])),[a]);self.assertEqual(list(store.json_rows(rb['revision'])),[b])
                source.unlink();(root/'archive-A/decisions.jsonl').unlink()
                store.db.execute('DELETE FROM sources');store.db.execute('DELETE FROM revisions');store.db.commit()
                self.assertEqual(store.rebuild_index(),{'revisions':2,'sources':2})
                self.assertEqual(list(store.json_rows(ra['revision'])),[a]);self.assertEqual(list(store.json_rows(rb['revision'])),[b])
                self.assertEqual({h['contract']['semantics'] for h in store.heads()},{'v1','v2'})

    def test_partial_tail_is_preserved_but_not_a_synthetic_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'data';p.write_bytes(b'{"a":1}\n{"b":')
            with EvidenceStore(root/'store',chunk_bytes=3) as s:
                a=s.capture(p,partition='r',relative='x.jsonl',contract={},append=True)
                self.assertEqual(b''.join(s.bytes(a['revision'])),p.read_bytes())
                self.assertEqual(list(s.json_rows(a['revision'])),[{'a':1}])
                with p.open('ab') as f:f.write(b'2}\n')
                b=s.capture(p,partition='r',relative='x.jsonl',contract={},append=True)
                self.assertEqual(list(s.json_rows(b['revision'])),[{'a':1},{'b':2}])
                self.assertEqual(list(s.json_rows(a['revision'])),[{'a':1}])
                self.assertEqual(b['new_bytes'],3)
                self.assertEqual(s.capture(p,partition='r',relative='x.jsonl',contract={},append=True)['state'],'UNCHANGED')

    def test_atomic_snapshot_and_in_place_truncation_keep_old_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'data';p.write_bytes(b'old-source-payload\n')
            with EvidenceStore(root/'store',chunk_bytes=4) as s:
                a=s.capture(p,partition='r',relative='x',contract={},append=True)
                p.write_bytes(b'new\n')
                b=s.capture(p,partition='r',relative='x',contract={},append=True)
                self.assertIsNone(s.revision(b['revision'])['previous_revision'])
                self.assertEqual(b''.join(s.bytes(a['revision'])),b'old-source-payload\n')
                self.assertEqual(b''.join(s.bytes(b['revision'])),b'new\n')

    def test_compression_reconstruction_and_corruption_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'data.gz';payload=b'{"source":"original"}\n'*5;p.write_bytes(gzip.compress(payload,mtime=0))
            with EvidenceStore(root/'store',chunk_bytes=10) as s:
                r=s.capture(p,partition='r',relative='data.jsonl.gz',contract={})
                self.assertEqual(b''.join(s.bytes(r['revision'])),payload)
                ref=s.revision(r['revision'])['chunks'][0];(s.root/ref['object']).write_bytes(gzip.compress(b'corrupted'))
                with self.assertRaisesRegex(ValueError,'hash mismatch'):b''.join(s.bytes(r['revision']))

    def test_resume_budget_and_model_independent_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'data';payload=b'a'*23;p.write_bytes(payload)
            with EvidenceStore(root/'store',chunk_bytes=5) as s:
                first=s.capture(p,partition='r',relative='x',contract={},append=True,maximum_bytes=10)
                self.assertFalse(first['capture_complete'])
                second=s.capture(p,partition='r',relative='x',contract={},append=True,maximum_bytes=10)
                third=s.capture(p,partition='r',relative='x',contract={},append=True,maximum_bytes=10)
                self.assertTrue(third['capture_complete']);self.assertEqual(b''.join(s.bytes(third['revision'])),payload)
                self.assertEqual(b''.join(s.bytes(first['revision'])),payload[:10])

    def test_catalog_retains_unclassified_and_does_not_follow_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Path(tmp);live=r/'live';live.mkdir();(live/'mystery.bytes').write_bytes(b'unknown')
            (live/'alias').symlink_to(live/'mystery.bytes')
            d=inventory({'live':live},r/'catalog')
            self.assertEqual(d['files'],2)
            self.assertEqual(d['source_families']['unclassified'],2)
            self.assertFalse(classify('mystery.bytes')['reproducible'])
            self.assertEqual(classify('ledger/execution.jsonl')['source_kind'],'CANONICAL_ECONOMIC_SOURCE')

if __name__=='__main__':unittest.main()
