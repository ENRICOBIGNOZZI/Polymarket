import gzip
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_evidence_store import EvidenceStore
from v7_lossless_data_compaction import compact_group,recompress_pack

class CompactionTests(unittest.TestCase):
    def test_recompression_preserves_aliases_and_old_cas_without_uninspected_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a=root/'closed.bin';a.write_bytes(bytes(1024*1024))
            with EvidenceStore(root/'store') as store:
                revision=store.capture(a,partition='A',relative=a.name,contract={})['revision']
                proof=compact_group([a],store,compressor=shutil.copyfile,check_closed=lambda _:True)
                pack=store.root/proof['pack_path']
                def sparse(source,target):
                    with target.open('wb') as out:out.truncate(source.stat().st_size)
                outside=root/'uninspected';os.link(pack,outside)
                with self.assertRaisesRegex(ValueError,'uninspected'):
                    recompress_pack(pack,[a],store,compressor=sparse,check_closed=lambda _:True)
                outside.unlink()
                result=recompress_pack(pack,[a],store,compressor=sparse,check_closed=lambda _:True)
                self.assertGreater(result['reclaimed_bytes'],0)
                self.assertEqual(pack.stat().st_ino,a.stat().st_ino)
                self.assertEqual(a.read_bytes(),bytes(1024*1024))
                store.rebuild_index()
                self.assertEqual(b''.join(store.bytes(revision)),a.read_bytes())

    def test_original_aliases_and_old_revisions_survive_shared_pack_and_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a=root/'closed.bin';b=root/'alias.bin';a.write_bytes(b'first-partial-record\nsecond\n');os.link(a,b)
            with EvidenceStore(root/'store',chunk_bytes=4) as store:
                r1=store.capture(a,partition='A',relative='closed.bin',contract={},append=True,maximum_bytes=7)['revision']
                r2=store.capture(a,partition='A',relative='closed.bin',contract={},append=True)['revision']
                expected=a.read_bytes()
                proof=compact_group([a,b],store,compressor=shutil.copyfile,check_closed=lambda _:True)
                self.assertEqual(a.read_bytes(),expected);self.assertEqual(b.read_bytes(),expected)
                self.assertEqual(a.stat().st_ino,b.stat().st_ino)
                self.assertEqual(a.stat().st_nlink,3)
                self.assertGreater(proof['reclaimed_object_bytes'],0)
                self.assertEqual(b''.join(store.bytes(r1)),expected[:7])
                self.assertEqual(b''.join(store.bytes(r2)),expected)
                rebound=store.capture(a,partition='A',relative='closed.bin',contract={},append=True,maximum_bytes=1)
                self.assertTrue(rebound['capture_complete'])
                self.assertEqual(rebound['new_bytes'],0)
                value=store.revision(rebound['revision'])
                self.assertTrue(value['previous_prefix_rebound_after_full_byte_verification'])
                self.assertEqual(value['previous_revision'],r2)
                a.unlink();b.unlink();store.rebuild_index()
                self.assertEqual(b''.join(store.bytes(r2)),expected)
                # A new capture cannot silently recreate the redundant gzip.
                ref=store.object(expected[:4]);self.assertTrue(ref['shared_immutable_pack'])
                self.assertEqual(store.read_object(ref),expected[:4])

    def test_gzip_whole_object_remains_decodable_and_corruption_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'closed.jsonl.gz';raw=b'{"market":"A"}\n';p.write_bytes(gzip.compress(raw,mtime=0))
            with EvidenceStore(root/'store') as store:
                r=store.capture(p,partition='A',relative=p.name,contract={})['revision']
                proof=compact_group([p],store,compressor=shutil.copyfile,check_closed=lambda _:True)
                self.assertEqual(b''.join(store.bytes(r)),raw)
                pack=store.root/proof['pack_path'];os.chmod(pack,0o600);pack.write_bytes(b'corrupt')
                with self.assertRaises((OSError,ValueError)):b''.join(store.bytes(r))

    def test_open_or_uninspected_alias_never_mutates_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'closed';p.write_bytes(b'original');q=root/'alias';os.link(p,q)
            with EvidenceStore(root/'store') as store:
                with self.assertRaisesRegex(ValueError,'aliases outside'):compact_group([p],store,compressor=shutil.copyfile,check_closed=lambda _:True)
                with self.assertRaisesRegex(ValueError,'source is open'):compact_group([p,q],store,compressor=shutil.copyfile,check_closed=lambda _:False)
                self.assertEqual(p.read_bytes(),b'original')

if __name__=='__main__':unittest.main()
