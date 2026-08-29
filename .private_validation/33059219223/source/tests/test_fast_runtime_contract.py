from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


class FastRuntimeContractTest(unittest.TestCase):
    def test_fast_engine_remains_shadow_only(self) -> None:
        policy = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["mode"], "shadow")
        self.assertFalse(policy["real_order_submission"])

        source = (ROOT / "src" / "fast_arb_main.cpp").read_text(encoding="utf-8")
        runtime = ROOT / "src" / "fast_runtime"
        source += "\n" + "\n".join(path.read_text(encoding="utf-8") for path in sorted(runtime.glob("part*.inc")))
        self.assertIn("wss://ws-subscriptions-clob.polymarket.com/ws/market", source)
        self.assertIn('"real_order_submission", false', source)
        for forbidden in ("PRIVATE_KEY", "--execute", "/order"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_singleton_launcher_is_the_common_single_owner_primitive(self) -> None:
        source = (ROOT / "scripts" / "runtime_singleton_launcher.py").read_text(encoding="utf-8")
        for token in (
            "fcntl.LOCK_EX | fcntl.LOCK_NB",
            "close_fds=True",
            "start_new_session=True",
            "pass_fds=(lock_fd,)",
            '"--internal-watchdog"',
            "os.getppid() != parent_pid",
            "_drain_process_group(child_pgid)",
            "os.set_inheritable(fd, False)",
            "LOCK_REACQUIRE_GRACE_SECONDS",
            "return 75",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_private_proof_is_exact_head_v7_neutral_and_isolated(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "private-runtime-single-writer-validation.yml").read_text(
            encoding="utf-8"
        )
        for token in (
            'ref: ${{ github.event.pull_request.head.sha || github.sha }}',
            'test "$sha" = "${{ github.event.pull_request.head.sha || github.sha }}"',
            'SOURCE_SHA: ${{ steps.source.outputs.sha }}',
            'root="$repo/.private_validation/$PROBE_ID"',
            'scripts/runtime_singleton_launcher.py',
            'second_owner_exit_code',
            'post_supervisor_loss_competitor_exit_code',
            'watchdog_holds_lock_after_supervisor_loss',
            'orphan_runtime_drained_before_reacquire',
            'live_runtime_started": False',
        ):
            with self.subTest(token=token):
                self.assertIn(token, workflow)
        lowered = workflow.lower()
        for retired in ("paper_v6_loop", "paper_latest_loop", "v6_hard_arb", "config/paper_v6", "v6_market_proxy"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, lowered)
        self.assertNotIn("scripts/run_paper.sh\" --", workflow)

    def test_runtime_singleton_excludes_competing_owner_and_reacquires(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = Path(tmpdir) / "runtime.lock"
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(launcher),
                    "--lock",
                    str(lock),
                    "--",
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                for _ in range(100):
                    if lock.exists() and lock.read_text(encoding="utf-8").strip():
                        break
                    if first.poll() is not None:
                        self.fail(f"first owner exited early rc={first.returncode}")
                    time.sleep(0.02)
                else:
                    self.fail("singleton lock was not materialized")

                second = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "print('unexpected')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(second.returncode, 75, second)
                self.assertIn("another paper runtime already owns", second.stderr)
            finally:
                first.terminate()
                try:
                    first.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.wait(timeout=5)
                for stream in (first.stdout, first.stderr):
                    if stream is not None:
                        stream.close()

            third = subprocess.run(
                [
                    sys.executable,
                    str(launcher),
                    "--lock",
                    str(lock),
                    "--",
                    sys.executable,
                    "-c",
                    "print('reacquired')",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(third.returncode, 0, third)
            self.assertEqual(third.stdout.strip(), "reacquired")

    def test_runtime_singleton_bridges_brief_lock_release_without_stealing(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = Path(tmpdir) / "runtime.lock"
            marker = Path(tmpdir) / "holder.ready"
            holder_code = (
                "import fcntl,os,sys,time; "
                "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o600); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "open(sys.argv[2],'w',encoding='utf-8').write('ready'); "
                "time.sleep(0.25); os.close(fd)"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code, str(lock), str(marker)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(100):
                    if marker.exists():
                        break
                    if holder.poll() is not None:
                        self.fail(f"brief lock holder exited early rc={holder.returncode}")
                    time.sleep(0.01)
                else:
                    self.fail("brief lock holder did not acquire the lock")

                result = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "print('bounded-handoff')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result)
                self.assertEqual(result.stdout.strip(), "bounded-handoff")
            finally:
                try:
                    holder.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.wait(timeout=2)

    def test_supervisor_sigkill_keeps_lock_until_orphan_runtime_is_drained(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = Path(tmpdir) / "runtime.lock"
            child_file = Path(tmpdir) / "child.pid"
            child_code = (
                "import os,signal,sys,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "open(sys.argv[1],'w',encoding='utf-8').write(str(os.getpid())); "
                "time.sleep(30)"
            )
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(launcher),
                    "--lock",
                    str(lock),
                    "--",
                    sys.executable,
                    "-c",
                    child_code,
                    str(child_file),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = 0
            try:
                for _ in range(150):
                    if lock.exists() and child_file.exists():
                        break
                    if first.poll() is not None:
                        self.fail(f"supervisor exited early rc={first.returncode}")
                    time.sleep(0.02)
                else:
                    self.fail("lock/child marker was not materialized")
                child_pid = int(child_file.read_text(encoding="utf-8"))
                self.assertTrue(_pid_alive(child_pid))

                first.kill()
                first.wait(timeout=5)
                self.assertTrue(_pid_alive(child_pid), "test must observe the orphan before watchdog drainage completes")

                blocked = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "print('unsafe-reacquire')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                self.assertEqual(blocked.returncode, 75, blocked)
                self.assertIn("another paper runtime already owns", blocked.stderr)

                self.assertTrue(
                    _wait_pid_gone(child_pid, 8.0),
                    "watchdog did not drain orphan runtime group before releasing ownership",
                )
                child_pid = 0

                reacquired = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "print('safe-reacquire')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(reacquired.returncode, 0, reacquired)
                self.assertEqual(reacquired.stdout.strip(), "safe-reacquire")
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait(timeout=5)
                if child_pid and _pid_alive(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
