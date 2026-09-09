import gzip
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_compressed_journal import CompressedJournal,compress_closed,journal_rows


class JournalTests(unittest.TestCase):
    def test_legacy_read_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'legacy.jsonl';path.write_text('{"value":1}\n')
            self.assertEqual(list(journal_rows(path)),[{'value':1}])
            self.assertEqual([p.name for p in path.parent.iterdir()],['legacy.jsonl'])

    def test_second_producer_is_rejected_until_first_owner_closes(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'journal.jsonl';first=CompressedJournal(path)
            try:
                with self.assertRaises(BlockingIOError):CompressedJournal(path)
                first.append({'owner':1})
            finally:first.close()
            with CompressedJournal(path) as second:second.append({'owner':2})
            self.assertEqual(list(journal_rows(path)),[{'owner':1},{'owner':2}])

    def test_rotation_compression_and_restart_preserve_ordered_exact_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'observations.jsonl';rows=[{'sequence':i,'payload':'x'*100} for i in range(12)]
            journal=CompressedJournal(path,256)
            for row in rows:
                journal.append(row)
                if journal.pending:journal.pending.result()
            journal.close()
            self.assertEqual(list(journal_rows(path)),rows)
            self.assertTrue(list(path.parent.glob('*.gz')))
            self.assertLess(path.stat().st_size if path.exists() else 0,256)
            journal=CompressedJournal(path,256);journal.append({'sequence':12});journal.close()
            self.assertEqual(list(journal_rows(path)),rows+[{'sequence':12}])

    def test_snapshot_does_not_lose_active_tail_during_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'observations.jsonl';journal=CompressedJournal(path,256)
            journal.append({'sequence':0,'payload':'x'*130})
            journal.append({'sequence':1,'payload':'x'*130})
            journal.pending.result()
            journal.append({'sequence':2})
            reader=journal_rows(path)
            self.assertEqual(next(reader)['sequence'],0)
            journal.append({'sequence':3,'payload':'x'*220})
            journal.pending.result();journal.close()
            self.assertEqual([r['sequence'] for r in reader],[1,2])
            self.assertEqual([r['sequence'] for r in journal_rows(path)],[0,1,2,3])

    def test_conflicting_archive_never_deletes_unique_closed_source(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'observations.jsonl.segment-0001.jsonl';raw=b'{"sequence":1}\n'
            source.write_bytes(raw);source.with_name(source.name+'.gz').write_bytes(gzip.compress(b'wrong'))
            with self.assertRaisesRegex(ValueError,'archive collision'):compress_closed(source)
            self.assertEqual(source.read_bytes(),raw)

    def test_slow_compression_fails_closed_before_unbounded_growth(self):
        with tempfile.TemporaryDirectory() as d:
            event=threading.Event();path=Path(d)/'observations.jsonl'
            def slow(source):event.wait(5);return compress_closed(source)
            with patch('v7_compressed_journal.compress_closed',side_effect=slow):
                journal=CompressedJournal(path,128)
                try:
                    journal.append({'payload':'x'*120})
                    journal.append({'payload':'y'*120})
                    with self.assertRaisesRegex(RuntimeError,'backlog'):journal.append({'payload':'z'*120})
                    self.assertLessEqual(path.stat().st_size,256)
                finally:event.set();journal.close()
            self.assertEqual(len(list(journal_rows(path))),2)


if __name__=='__main__':unittest.main()
