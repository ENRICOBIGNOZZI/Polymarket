from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('buffer_retention', ROOT / 'monitoring/v7_london_buffer_retention.py')
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


class LondonBufferObservationTests(unittest.TestCase):
    def config(self):
        cfg = json.loads((ROOT / 'config/v7_london_buffer_retention.json').read_text())
        # These legacy offload tests isolate file deletion from full-disk
        # accounting, which is exercised separately by the HFT tests.
        cfg.update(target_managed_bytes=1, maximum_managed_bytes=10_000,
                   account_all_run_files=False)
        return cfg

    def segment(self, root, name):
        path = root / 'external_fair/raw' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * 1024)
        old = time.time() - 7200
        os.utime(path, (old, old))
        return path

    def receipt(self, root, files):
        path = root / 'control/research_offload_receipt.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{'path': str(p.relative_to(root)), 'size': p.stat().st_size,
                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
        path.write_text(json.dumps({'schema': 'polymarket_v7_research_offload_receipt_v1',
                                    'synced_through_ns': time.time_ns(), 'files': rows}))

    def test_missing_receipt_reports_observed_bytes_without_deleting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.segment(root, 'x.segment-000001.bin')
            result = retention.run(root, self.config())
            self.assertEqual(result['state'], 'NO_VERIFIED_OFFLOAD')
            self.assertEqual(result['before_bytes'], 1024)
            self.assertEqual(result['after_bytes'], 1024)
            self.assertEqual(result['deleted'], [])
            self.assertTrue(path.exists())

    def test_missing_receipt_still_reports_buffer_limit_exceeded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.segment(root, 'x.segment-000001.bin')
            cfg = self.config(); cfg['maximum_managed_bytes'] = 100
            result = retention.run(root, cfg)
            self.assertEqual(result['state'], 'BUFFER_LIMIT_EXCEEDED_UNSYNCED_DATA_PRESERVED')
            self.assertEqual(result['after_bytes'], 1024)
            self.assertTrue(path.exists())

    def test_open_and_unsegmented_tapes_are_not_closed_by_a_copy_receipt(self):
        for name in ('x.segment-000001.bin.open', 'x.bin'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.segment(root, name)
                self.receipt(root, [path])
                result = retention.run(root, self.config())
                self.assertEqual(result['deleted'], [])
                self.assertEqual(result['after_bytes'], 1024)
                self.assertTrue(path.exists())

    def test_closed_verified_segments_still_prune(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.segment(root, 'x.segment-000001.bin')
            self.receipt(root, [path])
            result = retention.run(root, self.config())
            self.assertEqual(result['before_bytes'], 1024)
            self.assertEqual(result['after_bytes'], 0)
            self.assertFalse(path.exists())


def _runtime(root: Path, sha: str = "a" * 40) -> None:
    control = root / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "runtime_status.json").write_text(json.dumps({
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": sha,
    }))


class LondonLosslessRetentionTests(unittest.TestCase):
    def config(self):
        return json.loads((ROOT / 'config/v7_london_buffer_retention.json').read_text())

    def test_repricing_book_segments_are_counted_and_losslessly_compressed(self):
        from unittest import mock
        import gzip
        import v7_closed_tape_retention
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'paper_v7_live'
            _runtime(root)
            source = root / 'research/repricing_book/book_observations/session.segment-1000000.jsonl'
            source.parent.mkdir(parents=True)
            payload = (b'{"book":true,"levels":[1,2,3]}\n' * 5000)
            source.write_bytes(payload)
            old = time.time() - 120
            os.utime(source, (old, old))
            with mock.patch.object(v7_closed_tape_retention, '_tape_file_closed', return_value=True):
                result = retention.run(root, self.config())
            compressed = source.with_name(source.name + '.gz')
            self.assertFalse(source.exists())
            self.assertTrue(compressed.is_file())
            self.assertEqual(gzip.open(compressed, 'rb').read(), payload)
            self.assertGreaterEqual(result['before_bytes'], len(payload))
            self.assertLess(result['after_compression_bytes'], len(payload))
            self.assertEqual(len(result['lossless_compression']['archived']), 1)
            self.assertFalse(result['lossless_compression']['failures'])

    def test_old_compressed_detail_stays_pinned_without_replayable_hft_windows(self):
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'paper_v7_live'
            _runtime(root)
            compressed = root / 'research/repricing_book/book_observations/session.segment-1000000.jsonl.gz'
            compressed.parent.mkdir(parents=True)
            payload = b'raw-evidence\n' * 1000
            with gzip.GzipFile(filename='', mode='wb', fileobj=compressed.open('wb'), mtime=0) as handle:
                handle.write(payload)
            now = 100_000.0
            old = now - 9 * 3600
            os.utime(compressed, (old, old))
            result = retention.run(root, self.config(), now=now)
            self.assertTrue(compressed.exists())
            retired = result['rolling_retirement']['retired']
            self.assertEqual(retired, [])
            self.assertTrue(result['rolling_retirement']['failures'])

    def test_recent_compressed_detail_is_preserved(self):
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'paper_v7_live'
            _runtime(root)
            compressed = root / 'external_fair/raw/feed.segment-000001.bin.gz'
            compressed.parent.mkdir(parents=True)
            with gzip.GzipFile(filename='', mode='wb', fileobj=compressed.open('wb'), mtime=0) as handle:
                handle.write(b'recent')
            now = 100_000.0
            os.utime(compressed, (now - 60, now - 60))
            result = retention.run(root, self.config(), now=now)
            self.assertTrue(compressed.exists())
            self.assertEqual(result['rolling_retirement']['retired'], [])

    def test_real_production_segment_patterns_are_managed(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [
                root / 'research/repricing_book/book_observations/abc.segment-1000000.jsonl',
                root / 'external_fair/assets/eth/raw/binance.segment-000001.bin',
                root / 'external_fair/assets/eth/normalized_events/binance.segment-000001.bin.gz',
            ]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'x' * 17)
            rows = retention._managed(root, config)
            self.assertEqual({row[1] for row in rows}, {str(path.relative_to(root)) for path in files})
            self.assertEqual(retention._total(rows), 51)

    def test_production_archive_catchup_is_cold_path_and_within_service_timeout(self):
        config = self.config()
        self.assertEqual(config['hft_active_ingest_max_rows'], 50_000)
        self.assertGreater(config['hft_archive_ingest_max_rows'], config['hft_active_ingest_max_rows'])
        self.assertLess(config['hft_ingest_budget_seconds'], 900)


    def test_unhealthy_native_capture_is_safely_pinned_not_failed(self):
        from unittest import mock
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'paper_v7_live'
            _runtime(root)
            compressed = root / 'research/native_observations/run/capture.jsonl.gz'
            compressed.parent.mkdir(parents=True)
            with gzip.GzipFile(filename='', mode='wb', fileobj=compressed.open('wb'), mtime=0) as handle:
                handle.write(b'{"schema":"polymarket_v7_native_observation_v1"}\n')
            now = 100_000.0
            old = now - 9 * 3600
            os.utime(compressed, (old, old))
            cfg = self.config()
            cfg['account_all_run_files'] = False
            cfg['require_hft_window_preservation'] = True
            cfg['rolling_raw_detail_seconds'] = 7200

            class FakeWindows:
                def __init__(self, *args, **kwargs):
                    pass
                def ingest(self, **kwargs):
                    return {'records': 0, 'decisions': 0, 'failures': []}
                def preserve(self, path):
                    raise ValueError('UNHEALTHY_NATIVE_CAPTURE_PINNED')
                def close(self):
                    pass

            with mock.patch.object(retention, 'Windows', FakeWindows):
                result = retention.run(root, cfg, now=now)
            self.assertTrue(compressed.exists())
            self.assertEqual(result['rolling_retirement']['failures'], [])
            self.assertEqual(
                result['rolling_retirement']['pinned'],
                [{'source': str(compressed.relative_to(root)),
                  'reason': 'UNHEALTHY_NATIVE_CAPTURE_PINNED'}],
            )
            self.assertNotEqual(result['state'], 'RETENTION_PARTIAL_FAILURE')

    def test_corrupt_old_gzip_is_preserved_and_surfaces_partial_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'paper_v7_live'
            _runtime(root)
            compressed = root / 'research/repricing_book/book_observations/session.segment-1000000.jsonl.gz'
            compressed.parent.mkdir(parents=True)
            compressed.write_bytes(b'not-a-gzip-stream')
            now = 100_000.0
            old = now - 9 * 3600
            os.utime(compressed, (old, old))
            result = retention.run(root, self.config(), now=now)
            self.assertTrue(compressed.exists())
            self.assertEqual(result['state'], 'RETENTION_PARTIAL_FAILURE')
            self.assertEqual(len(result['rolling_retirement']['failures']), 1)
            receipt_dir = root / self.config()['rolling_receipt_directory']
            self.assertFalse(receipt_dir.exists())


if __name__ == '__main__':
    unittest.main()
