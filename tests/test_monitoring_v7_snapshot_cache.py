import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

import exporter_v7 as exporter  # noqa: E402


class SnapshotCacheTests(unittest.TestCase):
    def test_slow_refresh_never_blocks_readers(self) -> None:
        second_refresh_started = threading.Event()
        release_second_refresh = threading.Event()
        calls = 0

        def collect(_run_root: Path, _repository_root: Path) -> dict:
            nonlocal calls
            calls += 1
            if calls == 2:
                second_refresh_started.set()
                self.assertTrue(release_second_refresh.wait(3.0))
            return {
                "sequence": calls,
                "maker_fillability": {"sequence": calls},
                "external_fair": {"sequence": calls},
            }

        def render(snapshot: dict) -> str:
            return f"test_snapshot_sequence {snapshot['sequence']}\n"

        cache = exporter.SnapshotCache(Path("run"), ROOT, refresh_seconds=1.0)
        with mock.patch.object(exporter, "collect_snapshot", side_effect=collect), mock.patch.object(
            exporter, "render_prometheus", side_effect=render
        ):
            cache.start()
            self.assertTrue(cache.wait_ready(2.0))
            self.assertEqual(cache.read()["snapshot"]["sequence"], 1)
            self.assertTrue(second_refresh_started.wait(2.0))

            started = time.monotonic()
            during_refresh = cache.read()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.05)
            self.assertEqual(during_refresh["snapshot"]["sequence"], 1)

            release_second_refresh.set()
            deadline = time.monotonic() + 2.0
            while cache.read()["snapshot"]["sequence"] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(cache.read()["snapshot"]["sequence"], 2)
            cache.stop()


if __name__ == "__main__":
    unittest.main()
