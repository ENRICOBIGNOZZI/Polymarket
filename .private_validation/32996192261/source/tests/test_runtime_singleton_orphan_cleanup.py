from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSingletonOrphanCleanupTest(unittest.TestCase):
    def test_launcher_keeps_explicit_forwarding_and_post_wait_group_drain(self) -> None:
        source = (ROOT / "scripts" / "runtime_singleton_launcher.py").read_text(encoding="utf-8")
        self.assertIn("start_new_session=True", source)
        self.assertIn("os.killpg(child.pid, signum)", source)
        self.assertIn("group_id = child.pid", source)
        self.assertIn("_drain_child_group(group_id)", source)
        self.assertIn("signal.SIGTERM", source)
        self.assertIn("signal.SIGKILL", source)

    def test_direct_wrapper_exit_drains_lingering_group_descendant(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            lock = root / "runtime.lock"
            pid_file = root / "grandchild.pid"
            ready_file = root / "grandchild.ready"
            term_file = root / "grandchild.term"

            grandchild_code = r"""
import signal
import sys
import time
from pathlib import Path

term_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])

def stop(signum, _frame):
    term_path.write_text(str(signum), encoding="utf-8")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
ready_path.write_text("ready", encoding="utf-8")
while True:
    time.sleep(1)
"""
            direct_wrapper_code = r"""
import subprocess
import sys
import time
from pathlib import Path

code, term_path, ready_path, pid_path = sys.argv[1:]
child = subprocess.Popen([sys.executable, "-c", code, term_path, ready_path])
Path(pid_path).write_text(str(child.pid), encoding="utf-8")
for _ in range(200):
    if Path(ready_path).exists():
        break
    if child.poll() is not None:
        raise SystemExit("grandchild exited before readiness")
    time.sleep(0.01)
else:
    raise SystemExit("grandchild did not become ready")
# Exit deliberately without waiting for the descendant. The singleton launcher
# must drain the owned process group after this direct wrapper disappears.
"""

            grandchild_pid = 0
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        direct_wrapper_code,
                        grandchild_code,
                        str(term_file),
                        str(ready_file),
                        str(pid_file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=12,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result)
                self.assertTrue(pid_file.exists(), result)
                grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
                self.assertEqual(
                    term_file.read_text(encoding="utf-8").strip(),
                    str(signal.SIGTERM),
                    "the direct wrapper exited but its process-group descendant was not retired",
                )
            finally:
                if grandchild_pid:
                    try:
                        os.kill(grandchild_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
