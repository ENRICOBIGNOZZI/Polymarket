from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FastRuntimeContractTest(unittest.TestCase):
    def test_fast_engine_is_shadow_only_and_separate(self) -> None:
        policy = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["mode"], "shadow")
        self.assertFalse(policy["real_order_submission"])
        self.assertGreaterEqual(policy["min_net_edge"], 0.0005)
        self.assertGreater(policy["conversion_fixed_cost_usd"], 0.0)

        runtime = ROOT / "src" / "fast_runtime"
        source = (ROOT / "src" / "fast_arb_main.cpp").read_text(encoding="utf-8")
        source += "\n" + "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(runtime.glob("part*.inc"))
        )
        self.assertIn("wss://ws-subscriptions-clob.polymarket.com/ws/market", source)
        self.assertIn("fast_arb_status.json", source)
        self.assertIn('"real_order_submission", false', source)
        self.assertNotIn("PRIVATE_KEY", source)
        self.assertNotIn("--execute", source)
        self.assertNotIn("/order", source)

        transport = (ROOT / "src" / "fast_ws.cpp").read_text(encoding="utf-8")
        self.assertIn("boost/beast/websocket.hpp", transport)
        self.assertIn("SSL_set_tlsext_host_name", transport)
        self.assertIn("__has_include(<boost/asio/ssl/host_name_verification.hpp>)", transport)
        self.assertIn("boost/asio/ssl/host_name_verification.hpp", transport)
        self.assertIn("ssl::host_name_verification(endpoint.host)", transport)
        self.assertIn("ssl::rfc2818_verification(endpoint.host)", transport)
        self.assertIn("!defined(__APPLE__) && defined(__cpp_lib_jthread)", transport)
        self.assertIn("std::vector<std::thread> threads", transport)
        self.assertIn("fallback_stop_requested", transport)
        self.assertIn("if (thread.joinable()) thread.join();", transport)
        self.assertNotIn("curl_ws_", transport)

    def test_runtime_selector_supervises_fast_and_champion_planes(self) -> None:
        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn("polymarket_fast_arb_shadow", selector)
        self.assertIn('POLYMARKET_FAST_ARB_REQUIRED:-0', selector)
        self.assertIn("shadow_dependency_failure", selector)
        self.assertIn("champion continues without fast shadow", selector)
        self.assertIn("runtime_planes.csv", selector)
        self.assertIn("start_champion", selector)
        self.assertIn("start_fast", selector)
        self.assertIn("--print-champion", selector)

    def test_runtime_selector_acquires_singleton_before_starting_children(self) -> None:
        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn("runtime_singleton_launcher.py", selector)
        self.assertIn("runtime_owner.lock", selector)
        self.assertIn("POLYMARKET_RUNTIME_SINGLETON_HELD", selector)
        self.assertIn("runtime_handoff.request", selector)
        self.assertLess(selector.index("runtime_singleton_launcher.py"), selector.index("start_champion()"))
        # The private macOS server uses Bash 3.2, which does not provide BASHPID.
        self.assertIn('tmp="$path.tmp.${BASHPID:-$$}"', selector)

    def test_explicit_deploy_handoff_retires_only_verified_stale_runtime_owner(self) -> None:
        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
        self.assertIn('handoff_marker="$run_root/runtime_handoff.request"', selector)
        self.assertIn('reap_requested_stale_runtime_owner()', selector)
        self.assertIn('same_repository_runtime_owner()', selector)
        self.assertIn('same_repository_runtime_lock_holder()', selector)
        self.assertIn('runtime_tree_pids()', selector)
        self.assertIn('runtime_lock_is_free()', selector)
        self.assertIn('stale_runtime_owner_reaped=', selector)
        self.assertIn('stale_runtime_orphan_lock_holder_reaped=', selector)
        self.assertIn('stale_runtime_owner_killed=', selector)
        self.assertIn('runtime handoff refused unknown lock owner', selector)
        self.assertIn('runtime handoff refused unknown lock holder', selector)
        self.assertIn('runtime handoff refused unknown orphan lock holder', selector)
        self.assertIn('request_runtime_handoff()', updater)
        self.assertIn('clear_runtime_handoff()', updater)
        self.assertIn('runtime_handoff.request', updater)
        self.assertIn('request_runtime_handoff "$NEW_SHA"', updater)

    def test_v6_child_retires_when_the_runtime_wrapper_disappears(self) -> None:
        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        v6_loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn('POLYMARKET_RUNTIME_PARENT_PID="$$"', selector)
        self.assertIn('RUNTIME_PARENT_PID="${POLYMARKET_RUNTIME_PARENT_PID:-}"', v6_loop)
        self.assertIn('parent_runtime_alive(){', v6_loop)
        self.assertIn('runtime_parent_lost=1', v6_loop)
        self.assertLess(v6_loop.index('parent_runtime_alive(){'), v6_loop.index('while true;do'))

    def test_v6_startup_reaps_only_loop_outside_current_runtime_ancestry(self) -> None:
        v6_loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn('ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"', v6_loop)
        self.assertIn('is_current_runtime_descendant(){', v6_loop)
        self.assertIn('is_stale_v6_loop(){', v6_loop)
        self.assertIn('is_same_repository_v6_loop(){', v6_loop)
        self.assertIn('/bin/ps -o ppid=', v6_loop)
        self.assertIn('/bin/ps -o command=', v6_loop)
        self.assertIn('[[ "$pid" == "$RUNTIME_PARENT_PID" ]]', v6_loop)
        self.assertIn("pgrep -f 'paper_v6_loop\\.sh'", v6_loop)
        self.assertIn('lsof -a -p "$pid" -d cwd -Fn', v6_loop)
        self.assertIn('[[ "$cwd" == "$ROOT" ]]', v6_loop)
        self.assertIn('stale_v6_loop_reaped=', v6_loop)
        self.assertIn('reap_stale_v6_loops\nstart_proxy', v6_loop)

    def test_private_runtime_canary_exercises_stale_loop_handoff_and_fail_closed(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "private-runtime-single-writer-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('start_simulated_v6_loop(){', workflow)
        self.assertIn('start_simulated_legacy_runtime_owner()', workflow)
        self.assertIn('legacy_inherited_lock_child_pid=', workflow)
        self.assertIn('stale_runtime_owner_reaped=$stale_owner_pid', workflow)
        self.assertIn('private_orphan_lock_handoff', workflow)
        self.assertIn('stale_runtime_orphan_lock_holder_reaped=$orphan_child_pid', workflow)
        self.assertIn('singleton lock leaked after orphan handoff', workflow)
        self.assertIn('singleton lock leaked into descendants', workflow)
        self.assertIn('stale_v6_loop_reaped=$stale_pid', workflow)
        self.assertIn("historical relative `scripts/paper_v6_loop.sh` argv form", workflow)
        self.assertIn('fatal: stale V6 loop did not exit before startup', workflow)
        self.assertIn('resistant stale loop did not force fail-closed startup', workflow)
        self.assertIn('restart_accounting_identity=stable', workflow)

    def test_runtime_singleton_launcher_excludes_competing_owner(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        launcher_source = launcher.read_text(encoding="utf-8")
        self.assertIn("subprocess.Popen(", launcher_source)
        self.assertIn("close_fds=True", launcher_source)
        self.assertIn("start_new_session=True", launcher_source)
        self.assertIn("os.set_inheritable(fd, False)", launcher_source)
        self.assertIn("os.killpg(child.pid, signum)", launcher_source)
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
                for _ in range(100):
                    if lock.exists() and lock.read_text(encoding="utf-8").strip():
                        break
                    if first.poll() is not None:
                        self.fail(f"first singleton owner exited early rc={first.returncode}")
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

    def test_runtime_singleton_supervisor_does_not_leak_lock_to_descendants(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = Path(tmpdir) / "runtime.lock"
            child_pid_file = Path(tmpdir) / "legacy-child.pid"
            child_code = (
                "import os,signal,sys,time; "
                "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            )
            shell_command = (
                f"{shlex.quote(sys.executable)} -c {shlex.quote(child_code)} "
                f"{shlex.quote(str(child_pid_file))} & wait"
            )
            first = subprocess.Popen(
                [
                    sys.executable,
                    str(launcher),
                    "--lock",
                    str(lock),
                    "--",
                    "/usr/bin/env",
                    "bash",
                    "-c",
                    shell_command,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            child_pid = 0
            try:
                for _ in range(100):
                    if lock.exists() and lock.read_text(encoding="utf-8").strip() and child_pid_file.exists():
                        break
                    if first.poll() is not None:
                        self.fail(f"singleton supervisor exited early rc={first.returncode}")
                    time.sleep(0.02)
                else:
                    self.fail("singleton lock or child marker was not materialized")
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))

                # SIGKILL cannot be forwarded by the supervisor.  The legacy
                # shell child remains alive, so reacquisition proves that its
                # descriptor was not inherited from the singleton owner.
                first.kill()
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

    def test_v6_runtime_writers_use_unique_atomic_temp_paths(self) -> None:
        writers = [
            "scripts/v6_hard_arb_paper.py",
            "scripts/v6_micro_taker.py",
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
                self.assertIn(payload["index"], range(32))
                self.assertEqual(list(state.parent.glob("state.json.tmp.*")), [])
        finally:
            sys.path.pop(0)

    def test_hourly_operational_and_theory_schedulers_are_distinct(self) -> None:
        operational = (ROOT / ".github" / "workflows" / "fast-arb-hourly.yml").read_text(
            encoding="utf-8"
        )
        theory = (ROOT / ".github" / "workflows" / "arb-theory-hourly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "7 * * * *"', operational)
        self.assertIn("polymarket_fast_arb_shadow", operational)
        self.assertIn("validate_fast_data_health.py", operational)
        self.assertIn("--max-feed-stale-ms 45000", operational)
        self.assertIn("--min-rest-resyncs 2", operational)
        self.assertIn("arb_theory_scheduler.py", operational)
        self.assertIn('cron: "37 * * * *"', theory)
        self.assertIn("research/auto-fast-arb-policy", theory)
        self.assertIn("gh pr create --draft", theory)
        self.assertNotIn("gh pr merge", theory)
        self.assertNotIn("--admin", theory)

    def test_generated_candidate_is_fail_closed(self) -> None:
        generated = (ROOT / "include" / "pm" / "generated" / "fast_arb_candidate_policy.hpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("kRealOrderSubmission = false", generated)
        self.assertIn("kPromotionReady = false", generated)


if __name__ == "__main__":
    unittest.main()
