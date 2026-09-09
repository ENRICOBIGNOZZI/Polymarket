import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_compressed_journal import CompressedJournal,compress_closed,journal_rows
from v7_external_fair_paper_router import _CounterfactualIndex,PaperRouter,_paper_exploration_evidence_paths
from v7_external_rich_train import records as training_records
from v7_external_economic_common import discover_counterfactual_tapes
from v7_profitability_audit import counterfactual_paths


class JournalTests(unittest.TestCase):
    def test_gzip_publication_link_cleanup_is_not_a_content_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'counterfactuals.jsonl'
            sealed=path.with_name(path.name+'.segment-00000000000000000001.jsonl.gz')
            row={'record_id':'saved','event_type':'FORECAST','timestamp_ms':1,'model_sha':'a'*40}
            sealed.write_bytes(gzip.compress((json.dumps(row)+'\n').encode()))
            temporary=Path(d)/'compression-temporary';os.link(sealed,temporary)
            index=_CounterfactualIndex([path]);self.addCleanup(index.close)
            original=os.fstat;calls=[]
            def fstat(fd):
                calls.append(fd)
                if len(calls)==2:temporary.unlink()
                return original(fd)
            with patch('v7_external_fair_paper_router.os.fstat',side_effect=fstat):
                self.assertEqual(dict(index.iter_records()),{'saved':row})
            self.assertFalse(temporary.exists())

    def test_index_retries_compression_publication_without_omitting_closed_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'counterfactuals.jsonl'
            sealed=path.with_name(path.name+'.segment-00000000000000000001.jsonl')
            row={'record_id':'saved','event_type':'FORECAST','timestamp_ms':1,'model_sha':'a'*40}
            sealed.write_text(json.dumps(row)+'\n')
            index=_CounterfactualIndex([path]);self.addCleanup(index.close)
            original=index._guards;published=[]
            def guards(handle,offset):
                if not published:compress_closed(sealed);published.append(True)
                return original(handle,offset)
            with patch.object(index,'_guards',side_effect=guards):
                self.assertEqual(dict(index.iter_records()),{'saved':row})
            self.assertEqual(published,[True])
            self.assertTrue(all(p.suffix=='.gz' for p in index.physical_paths))

    def test_counterfactual_index_and_training_survive_rotation_and_missing_hot_tail(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'external_fair/counterfactuals.jsonl'
            expected=[{'record_id':str(i),'event_type':'FORECAST','model_sha':'a'*40,
                       'timestamp_ms':i,'value':'x'*100} for i in range(12)]
            index=_CounterfactualIndex([path]);self.addCleanup(index.close)
            with CompressedJournal(path,256) as journal:
                for i,row in enumerate(expected):
                    journal.append(row)
                    if journal.pending:journal.pending.result()
                    self.assertEqual(list(dict(index.iter_records()).values()),expected[:i+1])
                # Explicit rotation leaves only compressed history, no active file.
                self.assertFalse(path.exists())
                self.assertEqual(list(training_records([path]).values()),expected)
                self.assertEqual(dict(index.iter_records()),{r['record_id']:r for r in expected})
                self.assertEqual(index.metrics['last_bytes_read'],0)
                sources=discover_counterfactual_tapes([root])
                self.assertTrue(sources)
                self.assertTrue(all(p.suffix=='.gz' for p in sources))
                self.assertEqual(counterfactual_paths([root]),sources)
                self.assertEqual(discover_counterfactual_tapes([path]),sources)
            rebuilt=_CounterfactualIndex([path])
            try:self.assertEqual(list(dict(rebuilt.iter_records()).values()),expected)
            finally:rebuilt.close()

    def test_rotated_counterfactual_conflict_and_incomplete_closed_record_fail(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'counterfactuals.jsonl'
            original={'record_id':'r','event_type':'FORECAST','timestamp_ms':1,'model_sha':'a'*40}
            with CompressedJournal(path,64) as journal:
                journal.append(original);journal.pending.result()
            path.write_text(json.dumps({**original,'timestamp_ms':2})+'\n')
            index=_CounterfactualIndex([path])
            try:
                with self.assertRaisesRegex(RuntimeError,'conflict'):dict(index.iter_records())
            finally:index.close()
            with self.assertRaisesRegex(ValueError,'conflict'):training_records([path])
            path.unlink()
            sealed=path.with_name(path.name+'.segment-99999999999999999999.jsonl.gz')
            sealed.write_bytes(gzip.compress(b'{"record_id":"partial"}'))
            with self.assertRaisesRegex(ValueError,'incomplete'):list(journal_rows(path))
            index=_CounterfactualIndex([path])
            try:
                with self.assertRaisesRegex(RuntimeError,'incomplete'):dict(index.iter_records())
            finally:index.close()

    def test_router_model_switch_keeps_both_compressed_evidence_generations(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);config=Path(__file__).resolve().parents[1]/'config/v7_external_fair.json'
            first=PaperRouter(root,'a'*40,config,'https://invalid','https://invalid')
            first.enable_compressed_counterfactuals(512)
            try:
                for i in range(4):
                    first.emit_counterfactual('OPPORTUNITY_SET',counterfactual_id=str(i),opportunity_id=str(i))
                    for journal in first.counterfactual_journals.values():
                        if journal.pending:journal.pending.result()
                # Simulate interruption between active and durable publication.
                extra={**list(journal_rows(first.counterfactual_path))[-1],
                       'record_id':'active-only','counterfactual_id':'recovered','opportunity_id':'recovered'}
                first.counterfactual_journals[first.counterfactual_path].append(extra)
            finally:first.close_counterfactuals()
            before=list(journal_rows(first.durable_counterfactual_path))
            with ExitStack() as stack:
                journals={path:stack.enter_context(CompressedJournal(path,512))
                          for path in _paper_exploration_evidence_paths(root)}
                second=PaperRouter(root,'b'*40,config,'https://invalid','https://invalid',counterfactual_journals=journals)
                second.emit_counterfactual('OPPORTUNITY_SET',counterfactual_id='new',opportunity_id='new')
                for journal in second.counterfactual_journals.values():
                    if journal.pending:journal.pending.result()
            after=list(journal_rows(second.durable_counterfactual_path))
            self.assertEqual(after[:4],before)
            self.assertEqual(after[4],extra)
            self.assertEqual(len(after),6)
            records=PaperRouter.read_counterfactual_records(second.evidence_source_paths())
            self.assertEqual(len(records),6)
            self.assertEqual({r['model_sha'] for r in records.values()},{'a'*40,'b'*40})
            self.assertFalse((root/'ledger/execution.jsonl').exists())

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
