from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "v6_multileg_launcher.py"
LOCKER = r"""
import fcntl
import os
import sys
import time

path = sys.argv[1]
fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()}\n".encode())
sys.stdout.write("ready\n")
sys.stdout.flush()
time.sleep(30)
"""


class V6MultilegLauncherRecoveryTest(unittest.TestCase):
    def _start_locker(self, lock: Path, marker: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", LOCKER, str(lock), marker],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "ready")
        self.assertEqual(int(lock.read_text(encoding="utf-8").strip()), process.pid)
        return process

    def _stop(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def _run_launcher(self, lock: Path, runtime_parent_pid: int) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["POLYMARKET_RUNTIME_PARENT_PID"] = str(runtime_parent_pid)
        return subprocess.run(
            [
                sys.executable,
                str(LAUNCHER),
                "--lock",
                str(lock),
                "--",
                sys.executable,
                "-c",
                "print('reacquired')",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_reaps_verified_same_repo_stale_broker_owner(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmpdir:
            lock = Path(tmpdir) / "multileg.lock"
            stale = self._start_locker(lock, "polymarket_multileg_paper")
            unrelated_parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                result = self._run_launcher(lock, unrelated_parent.pid)
                self.assertEqual(result.returncode, 0, result)
                self.assertEqual(result.stdout.strip(), "reacquired")
                self.assertIn(f"stale_v6_multileg_owner_reaped={stale.pid}", result.stderr)
                stale.wait(timeout=3)
            finally:
                self._stop(stale)
                self._stop(unrelated_parent)

    def test_does_not_kill_unknown_lock_owner(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmpdir:
            lock = Path(tmpdir) / "multileg.lock"
            owner = self._start_locker(lock, "not-the-paper-broker")
            unrelated_parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                result = self._run_launcher(lock, unrelated_parent.pid)
                self.assertEqual(result.returncode, 75, result)
                self.assertIsNone(owner.poll())
                self.assertNotIn("stale_v6_multileg_owner_reaped=", result.stderr)
            finally:
                self._stop(owner)
                self._stop(unrelated_parent)

    def test_does_not_kill_owner_in_current_runtime_ancestry(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as tmpdir:
            lock = Path(tmpdir) / "multileg.lock"
            owner = self._start_locker(lock, "polymarket_multileg_paper")
            try:
                result = self._run_launcher(lock, os.getpid())
                self.assertEqual(result.returncode, 75, result)
                self.assertIsNone(owner.poll())
                self.assertNotIn("stale_v6_multileg_owner_reaped=", result.stderr)
            finally:
                self._stop(owner)


if __name__ == "__main__":
    unittest.main()
