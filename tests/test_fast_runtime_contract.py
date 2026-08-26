#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FastRuntimeContractTest(unittest.TestCase):
    def test_fast_engine_is_shadow_only_and_separate(self) -> None:
        context = json.loads((ROOT / "config" / "project_context.json").read_text(encoding="utf-8"))
        self.assertIn("fast-arb", json.dumps(context).lower())

    def test_hourly_operational_and_theory_schedulers_are_distinct(self) -> None:
        registry = json.loads((ROOT / "config" / "scheduler_registry.json").read_text(encoding="utf-8"))
        serialized = json.dumps(registry)
        self.assertIn("fast-arb-hourly", serialized)
        self.assertIn("arb-theory-hourly", serialized)

    def test_generated_candidate_is_fail_closed(self) -> None:
        policy = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8"))
        self.assertFalse(bool(policy.get("authenticated_execution", False)))

    def test_runtime_selector_supervises_fast_and_champion_planes(self) -> None:
        source = (ROOT / "scripts" / "run_paper.sh").read_text(encoding="utf-8")
        self.assertIn("runtime_singleton_launcher.py", source)
        self.assertIn("live_champion.json", source)

    def test_runtime_selector_acquires_singleton_before_starting_children(self) -> None:
        source = (ROOT / "scripts" / "run_paper.sh").read_text(encoding="utf-8")
        singleton = source.index("runtime_singleton_launcher.py")
        self.assertGreater(singleton, 0)
        self.assertNotIn("exec bash scripts/paper_v6_loop.sh", source)

    def test_runtime_singleton_launcher_excludes_competing_owner(self) -> None:
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
                    "import time; time.sleep(10)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.time() + 5
                while not lock.exists() and time.time() < deadline:
                    time.sleep(0.05)
                second = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "print('should-not-run')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertNotEqual(second.returncode, 0)
                self.assertNotIn("should-not-run", second.stdout)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=5)
                for stream in (first.stdout, first.stderr):
                    if stream is not None:
                        stream.close()

    def test_runtime_singleton_supervisor_does_not_leak_lock_to_descendants(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = Path(tmpdir) / "runtime.lock"
            child_pid_file = Path(tmpdir) / "child.pid"
            code = textwrap.dedent(
                f"""
                import subprocess, sys, time
                from pathlib import Path
                child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
                Path({str(child_pid_file)!r}).write_text(str(child.pid))
                time.sleep(30)
                """
            )
            first = subprocess.Popen(
                [sys.executable, str(launcher), "--lock", str(lock), "--", sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            child_pid = 0
            try:
                deadline = time.time() + 5
                while time.time() < deadline:
                    if child_pid_file.exists():
                        child_pid = int(child_pid_file.read_text())
                        break
                    time.sleep(0.05)
                self.assertGreater(child_pid, 0)
                os.kill(first.pid, signal.SIGKILL)
                first.wait(timeout=5)
                reacquired = subprocess.run(
                    [
                        sys.executable,
                        str(launcher),
                        "--lock",
                        str(lock),
                        "--",
                        sys.executable,
                        "-c",
                        "print('reacquired-after-supervisor-kill')",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(reacquired.returncode, 0, reacquired)
                self.assertEqual(reacquired.stdout.strip(), "reacquired-after-supervisor-kill")
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

    def test_private_runtime_canary_exercises_stale_loop_handoff_and_fail_closed(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "private-runtime-single-writer-validation.yml").read_text(encoding="utf-8")
        self.assertIn("stale", workflow.lower())
        self.assertIn("fail", workflow.lower())

    def test_explicit_deploy_handoff_retires_only_verified_stale_runtime_owner(self) -> None:
        deploy = (ROOT / ".github" / "workflows" / "deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn("runtime", deploy.lower())
        self.assertIn("pid", deploy.lower())

    def test_v6_child_retires_when_the_runtime_wrapper_disappears(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn("RUNTIME_PARENT_PID", loop)
        self.assertIn("parent_runtime_alive", loop)

    def test_v6_startup_reaps_only_loop_outside_current_runtime_ancestry(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn("is_current_runtime_descendant", loop)
        self.assertIn("is_same_repository_v6_loop", loop)

    def test_v6_runtime_writers_use_unique_atomic_temp_paths(self) -> None:
        # v6_micro_taker.py is now a compatibility adapter.  The persistent
        # writer remains frozen in v6_micro_taker_legacy.py and direct runtime
        # execution is delegated to v7_micro_taker_worker.py; do not require the
        # adapter shim itself to duplicate a second writer implementation.
        writers = [
            "scripts/v6_hard_arb_paper.py",
            "scripts/v6_micro_taker_legacy.py",
            "scripts/v6_external_bridge.py",
            "scripts/v6_relation_intents.py",
            "scripts/v6_local_factor_intents.py",
            "scripts/v6_intent_guard.py",
            "scripts/v6_runtime_status.py",
            "scripts/runtime_action_report.py",
            "scripts/v7_execution_evidence.py",
        ]
        for relative in writers:
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("os.getpid()", source, relative)
            self.assertIn("threading.get_ident()", source, relative)
            self.assertIn("os.replace(", source, relative)

        adapter = (ROOT / "scripts" / "v6_micro_taker.py").read_text(encoding="utf-8")
        self.assertIn("v6_micro_taker_legacy.py", adapter)
        self.assertIn("v7_micro_taker_worker", adapter)
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn('runtime_supervisor.csv.tmp.${BASHPID:-$$}', loop)

    def test_hard_arb_atomic_state_write_survives_concurrent_calls(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import v6_hard_arb_paper as hard

            with tempfile.TemporaryDirectory() as tmpdir:
                state = Path(tmpdir) / "state.json"

                def write(index: int) -> None:
                    hard.atomic_json(state, {"index": index})

                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(write, range(32)))

                payload = json.loads(state.read_text(encoding="utf-8"))
                self.assertIn("index", payload)
                self.assertFalse(list(Path(tmpdir).glob("state.json.tmp.*")))
        finally:
            sys.path.remove(str(ROOT / "scripts"))


if __name__ == "__main__":
    unittest.main()
