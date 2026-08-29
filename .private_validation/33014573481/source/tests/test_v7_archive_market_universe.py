from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_archive_market_universe as archive


class PointInTimeUniverseArchiveTest(unittest.TestCase):
    def cache(self, ts: int, market_id: str = "1", liquidity: float = 100.0) -> dict:
        return {
            "schema": archive.SOURCE_SCHEMA,
            "timestamp": ts,
            "source": "gamma",
            "markets": [
                {
                    "id": market_id,
                    "conditionId": f"c-{market_id}",
                    "question": f"market {market_id}",
                    "liquidityNum": liquidity,
                }
            ],
        }

    def write_cache(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def read_snapshot(self, path: Path) -> dict:
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))

    def test_first_snapshot_in_bucket_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache.json"
            out = root / "archive"
            self.write_cache(cache, self.cache(1801, "first"))
            first = archive.archive_once(cache, out, cadence_seconds=1800, retention_days=45, now_ts=2000)
            self.assertTrue(first["created"])
            target = Path(first["target"])
            self.assertEqual(self.read_snapshot(target)["markets"][0]["id"], "first")

            self.write_cache(cache, self.cache(3500, "later-survivor"))
            second = archive.archive_once(cache, out, cadence_seconds=1800, retention_days=45, now_ts=3500)
            self.assertFalse(second["created"])
            self.assertEqual(Path(second["target"]), target)
            self.assertEqual(self.read_snapshot(target)["markets"][0]["id"], "first")

    def test_new_bucket_creates_new_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache.json"
            out = root / "archive"
            self.write_cache(cache, self.cache(1801, "a"))
            a = archive.archive_once(cache, out, cadence_seconds=1800, now_ts=1801)
            self.write_cache(cache, self.cache(3601, "b"))
            b = archive.archive_once(cache, out, cadence_seconds=1800, now_ts=3601)
            self.assertTrue(a["created"])
            self.assertTrue(b["created"])
            self.assertNotEqual(a["target"], b["target"])
            self.assertEqual(len(list(out.glob("universe-*.json.gz"))), 2)

    def test_retention_is_bounded_and_only_archive_files_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache.json"
            out = root / "archive"
            out.mkdir()
            old = out / archive.snapshot_filename(1000)
            old.write_bytes(archive.encoded_snapshot({"x": 1}))
            keep = out / "do-not-touch.txt"
            keep.write_text("keep", encoding="utf-8")
            now = 1000 + 46 * 86400
            self.write_cache(cache, self.cache(now, "fresh"))
            summary = archive.archive_once(cache, out, retention_days=45, now_ts=now)
            self.assertFalse(old.exists())
            self.assertTrue(keep.exists())
            self.assertEqual(summary["removed_expired_snapshots"], 1)

    def test_snapshot_contains_source_timestamp_and_market_rows(self) -> None:
        payload = self.cache(7777, "m7")
        bucket, frozen = archive.archive_payload(payload, 1800)
        self.assertEqual(bucket, 7200)
        self.assertEqual(frozen["schema"], archive.ARCHIVE_SCHEMA)
        self.assertEqual(frozen["snapshot_timestamp"], 7777)
        self.assertEqual(frozen["bucket_timestamp"], 7200)
        self.assertEqual(frozen["market_count"], 1)
        self.assertEqual(frozen["markets"][0]["id"], "m7")

    def test_invalid_cache_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            archive.validate_cache({"schema": "wrong", "timestamp": 1, "markets": []})
        with self.assertRaises(ValueError):
            archive.validate_cache({"schema": archive.SOURCE_SCHEMA, "timestamp": 1, "markets": [{}]})


if __name__ == "__main__":
    unittest.main()
