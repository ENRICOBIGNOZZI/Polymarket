from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "v6_multileg_singleton.sh"
LOOP = ROOT / "scripts" / "paper_v6_loop.sh"


class V6MultiLegSingletonTests(unittest.TestCase):
    def test_live_loop_uses_singleton_wrapper(self) -> None:
        loop = LOOP.read_text(encoding="utf-8")
        self.assertIn('bash scripts/v6_multileg_singleton.sh "$RUN_ROOT" ./build/polymarket_multileg_paper', loop)
        self.assertEqual(loop.count("./build/polymarket_multileg_paper"), 1)

    def test_second_broker_is_rejected_while_owner_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = subprocess.Popen(
                ["bash", str(WRAPPER), td, "bash", "-c", "sleep 2"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            pid_file = Path(td) / ".multileg_broker.lock" / "pid"
            deadline = time.time() + 2.0
            while time.time() < deadline and not pid_file.exists():
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "singleton owner PID was not published")

            second = subprocess.run(
                ["bash", str(WRAPPER), td, "bash", "-c", "exit 0"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual(second.returncode, 75)
            self.assertIn("already running", second.stderr)
            self.assertIsNone(first.poll(), "second launch must not terminate the incumbent broker")
            first.terminate()
            first.wait(timeout=3)

    def test_stale_lock_is_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / ".multileg_broker.lock"
            lock.mkdir()
            (lock / "pid").write_text("99999999\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(WRAPPER), td, "bash", "-c", "exit 0"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_exec_preserves_pid_as_broker_owner(self) -> None:
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("printf '%s\\n' \"$$\" > \"$pid_file\"", text)
        self.assertIn('exec "$@"', text)
        self.assertNotIn("rm -f \"$pid_file\"", text)


if __name__ == "__main__":
    unittest.main()
