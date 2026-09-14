"""Replay the production labeler on the same continuous tape at two watermarks."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from v7_pm_repricing_labeler import Labeler
from v7_causal_book import SCHEMA

SHA = 'a' * 40
BASE = 1_780_000_000_000

class Capture:
    def __init__(self):
        self.rows = []
    def append(self, row):
        self.rows.append(row)


def replay(status_period_ms):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        args = SimpleNamespace(
            inference=root/'inference.jsonl', book_tape=root/'book.jsonl',
            book_status=root/'book-status.json', output=root/'labels.jsonl',
            status=root/'status.json', model_sha=SHA, horizon_ms=250,
            label_grace_ms=75, interval_ms=5, maximum_hot_bytes=64*1024**2)
        labeler = Labeler(args)
        labeler.journal.close()
        labeler.journal = Capture()
        # Feed production BookTimeline.ingest directly; no filesystem lag is
        # needed to reproduce publication-only censoring.
        labeler.book.poll = lambda: None
        seq = 0
        total = 0
        for offset in range(0, 2451, 5):
            stamp = BASE + offset
            for token in ('yes', 'no'):
                seq += 1
                labeler.book.ingest({
                    'schema': SCHEMA, 'model_sha': SHA, 'paper_only': True,
                    'authenticated_execution': False, 'real_order_submission': False,
                    'execution_authority': 'ZERO_AUTHORITY_RESEARCH_ONLY',
                    'observer_session_id': 'continuous', 'connection_epoch': 1,
                    'observer_sequence': seq, 'market_id': 'm', 'token_id': token,
                    'receive_wall_ms': stamp, 'receive_monotonic_ns': stamp*1000000,
                    'exchange_event_ns': stamp*1000000, 'valid': True,
                    'lineage_continuous': True, 'best_bid': .49, 'best_ask': .51,
                    'tick_size': .01})
            if offset % status_period_ms == 0:
                args.book_status.write_text(json.dumps({
                    'model_sha': SHA, 'paper_only': True, 'authenticated_execution': False,
                    'real_order_submission': False, 'state': 'running', 'evidence_complete': True,
                    'observer_session_id': 'continuous', 'connection_epoch': 1,
                    'timestamp_ms': stamp, 'book_events_written': seq,
                    'book_watermark_receive_wall_ms': stamp}))
            if 100 <= offset < 2100 and offset % 10 == 0:
                origin_id = str(offset)
                labeler.pending[origin_id] = {
                    'origin_id': origin_id, 'market_id': 'm', 'yes_token': 'yes',
                    'no_token': 'no', 'origin_observed_wall_ns': stamp*1000000,
                    'origin_pm_yes': .5, 'predicted_delta_probability': 0.,
                    'yes_tick_size': .01, 'no_tick_size': .01}
                total += 1
            with patch('time.time_ns', return_value=stamp*1000000):
                labeler.label()
        assert not labeler.pending
        rows = labeler.journal.rows
        assert len(rows) == total
        return {'origins': total, 'observed': labeler.labels,
                'censored': labeler.censored, 'coverage': labeler.labels/total}


class WatermarkCadenceTests(unittest.TestCase):
    def test_one_second_status_censors_continuous_book(self):
        result = replay(1000)
        self.assertLessEqual(result['coverage'], .10)
        self.assertGreater(result['observed'], 0)

    def test_25ms_status_recovers_same_tape_without_changing_grace(self):
        result = replay(25)
        self.assertEqual(result['coverage'], 1.)
        self.assertEqual(result['censored'], 0)

    def test_fast_watermark_is_separate_from_membership_and_flow(self):
        text = (ROOT/'src/v7_maker_fillability_observer.cpp').read_text()
        self.assertIn('options.fair_only ? 25 : 1000', text)
        self.assertIn('if (publish_flow) write_flow_snapshot', text)
        self.assertIn('now - last_membership_check_ms >= 1000', text)

if __name__ == '__main__':
    print('CONTROLLED_REPLAY', json.dumps({'old_1000ms': replay(1000), 'new_25ms': replay(25)}))
    unittest.main()
