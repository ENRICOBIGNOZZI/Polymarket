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
        cfg.update(target_managed_bytes=1, maximum_managed_bytes=10_000)
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


if __name__ == '__main__':
    unittest.main()
