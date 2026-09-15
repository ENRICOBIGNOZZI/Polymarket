from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_repricing_book_archive import archive_segments


class RepricingBookArchiveTest(unittest.TestCase):
    def test_sealed_segment_is_archived_and_source_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            folder = run_root / "research/repricing_book/book_observations"
            folder.mkdir(parents=True)
            segment = folder / "session.segment-1000000.jsonl"
            payload = (b'{"schema":"polymarket_v7_causal_book_observation_v1"}\n' * 100)
            segment.write_bytes(payload)
            current = folder / "current.jsonl"
            current.write_bytes(b"active\n")

            result = archive_segments(run_root)

            self.assertEqual(result["failures"], [])
            self.assertEqual(result["segments_seen"], 1)
            self.assertEqual(result["segments_verified"], 1)
            self.assertTrue(segment.exists())
            self.assertEqual(segment.read_bytes(), payload)
            self.assertEqual(current.read_bytes(), b"active\n")
            archived = Path(result["archives"][0]["archive"])
            self.assertEqual(gzip.open(archived, "rb").read(), payload)
            self.assertTrue(result["archives"][0]["source_preserved"])
            self.assertFalse(result["source_deletion_permitted"])

    def test_existing_corrupt_archive_fails_closed_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            folder = run_root / "research/repricing_book/book_observations"
            folder.mkdir(parents=True)
            segment = folder / "session.segment-1000000.jsonl"
            payload = b"good-source\n"
            segment.write_bytes(payload)

            first = archive_segments(run_root)
            archived = Path(first["archives"][0]["archive"])
            archived.write_bytes(gzip.compress(b"wrong"))

            second = archive_segments(run_root)
            self.assertEqual(second["segments_seen"], 1)
            self.assertEqual(len(second["failures"]), 1)
            self.assertEqual(segment.read_bytes(), payload)

    def test_current_file_and_unsealed_names_are_never_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            folder = run_root / "research/repricing_book/book_observations"
            folder.mkdir(parents=True)
            (folder / "current.jsonl").write_text("active\n")
            (folder / "legacy.jsonl").write_text("legacy\n")
            result = archive_segments(run_root)
            self.assertEqual(result["segments_seen"], 0)
            self.assertEqual(result["archives"], [])
            self.assertEqual(result["failures"], [])


if __name__ == "__main__":
    unittest.main()
