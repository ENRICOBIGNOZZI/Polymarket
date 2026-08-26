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
            "os.set_inheritable(fd, False)",
            "os.killpg(child.pid, signum)",
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
            'descriptor_not_inherited',
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
                    first.wait(timeout=5)
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

    def test_singleton_descriptor_is_not_inherited_by_child(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = Path(tmpdir) / "runtime.lock"
            child_file = Path(tmpdir) / "child.pid"
            child_code = (
                "import os,sys,time; "
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            child_pid = 0
            try:
                for _ in range(100):
                    if lock.exists() and child_file.exists():
                        break
                    if first.poll() is not None:
                        self.fail(f"supervisor exited early rc={first.returncode}")
                    time.sleep(0.02)
                else:
                    self.fail("lock/child marker was not materialized")
                child_pid = int(child_file.read_text(encoding="utf-8"))

                first.kill()
                first.wait(timeout=5)
                os.kill(child_pid, 0)

                reacquired = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "print('reacquired-with-orphan-child')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(reacquired.returncode, 0, reacquired)
                self.assertEqual(reacquired.stdout.strip(), "reacquired-with-orphan-child")
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait(timeout=5)
                if child_pid:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for stream in (first.stdout, first.stderr):
                    if stream is not None:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
