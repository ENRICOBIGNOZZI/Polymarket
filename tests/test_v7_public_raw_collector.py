import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT / "scripts"))
import v7_public_raw_collector as collector  # noqa: E402


class PublicRawCollectorTests(unittest.TestCase):
    def test_public_payload_is_content_addressed_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); payload = b'{"public":true}'
            result = collector.collect(root, run_id="run", source="gamma", url="https://example.test/markets", fetch=lambda _: payload, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            self.assertEqual(result["raw_payload_hash"], hashlib.sha256(payload).hexdigest())
            self.assertTrue((root / result["raw_payload_location"]).is_file())
            self.assertTrue((root / result["manifest_location"]).is_file())
            for field in ("exchange_timestamp", "source_observation_timestamp", "local_kernel_receive_timestamp",
                          "local_wall_receive_timestamp", "parse_complete_timestamp", "publish_timestamp"):
                self.assertIn(field, result)
            collector.collect(root, run_id="run", source="gamma", url="https://example.test/markets", fetch=lambda _: payload, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            second = collector.collect(root, run_id="run", source="gamma", url="https://example.test/other", fetch=lambda _: payload, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            self.assertNotEqual(result["manifest_hash"], second["manifest_hash"])
            with self.assertRaisesRegex(collector.PublicCollectorError, "collector_identity"):
                collector.collect(root, run_id="../bad", source="gamma", url="https://example.test", fetch=lambda _: payload)


if __name__ == "__main__": unittest.main()
