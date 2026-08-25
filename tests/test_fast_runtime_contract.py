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
        # The user-authorized PAPER research floor is 0.5 bp after executable costs.
        # Keep a strictly positive floor while permitting the bounded shadow experiment.
        self.assertGreaterEqual(policy["min_net_edge"], 0.00005)
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
        self.assertIn('reap_stale_v6_loops\nreap_stale_v6_proxy_listener\nreap_stale_v6_brokers\nstart_proxy', v6_loop)
        self.assertIn('stale_v6_proxy_listener_reaped=', v6_loop)
        self.assertIn('proxy_pid_owns_port', v6_loop)

    def test_private_runtime_canary_exercises_stale_loop_handoff_and_fail_closed(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "private-runtime-single-writer-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Validate stale-owner handoff before deploy", workflow)
        self.assertIn("Validate failed deploy leaves singleton runtime after rollback", workflow)
        self.assertIn("simulate_runtime_handoff_canary", workflow)
        self.assertIn("simulate_failed_deploy_rollback_canary", workflow)

    def test_launchd_service_owns_one_runtime_wrapper(self) -> None:
        control = (ROOT / "ops" / "macos_service_control.sh").read_text(encoding="utf-8")
        self.assertIn("polymarket-paper", control)
        self.assertIn("paper_latest_loop.sh", control)
        self.assertNotIn("paper_v6_loop.sh", control)

    def test_non_champion_loops_remain_shadow_or_research_only(self) -> None:
        for path in sorted((ROOT / "scripts").glob("paper_*_loop.sh")):
            if path.name == "paper_latest_loop.sh":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_KEY", text, path.name)
            self.assertNotIn("--execute", text, path.name)

    def test_runtime_uses_version_agnostic_monitoring(self) -> None:
        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn("monitoring/exporter_latest.py", selector)
        self.assertIn("monitoring/grafana/dashboards/polymarket-latest.json", selector)

    def test_fast_shadow_can_be_disabled_without_champion_mutation(self) -> None:
        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn('POLYMARKET_FAST_ARB_ENABLED:-1', selector)
        self.assertIn('fast_enabled=0', selector)
        self.assertIn('champion continues without fast shadow', selector)

    def test_fast_runtime_has_no_authenticated_submission_surface(self) -> None:
        sources = [ROOT / "src" / "fast_arb_main.cpp", ROOT / "src" / "fast_ws.cpp"]
        sources.extend(sorted((ROOT / "src" / "fast_runtime").glob("part*.inc")))
        text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        forbidden = ("PRIVATE_KEY", "POLYMARKET_PRIVATE_KEY", "--execute", "/order", "create_order", "post_order")
        for token in forbidden:
            self.assertNotIn(token, text)

    def test_fast_policy_conversion_cost_remains_positive(self) -> None:
        policy = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8"))
        self.assertGreater(policy["conversion_fixed_cost_usd"], 0.0)
        self.assertGreaterEqual(policy["slippage_bps"], 0.0)
        self.assertGreaterEqual(policy["latency_penalty_bps"], 0.0)

    def test_runtime_singleton_lock_cannot_be_stolen_by_live_process(self) -> None:
        launcher = ROOT / "scripts" / "runtime_singleton_launcher.py"
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "runtime_owner.lock"
            cmd = [sys.executable, str(launcher), "--lock", str(lock), "--", "sleep", "30"]
            first = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.time() + 5
                while time.time() < deadline and not lock.exists():
                    time.sleep(0.05)
                self.assertTrue(lock.exists())
                second = subprocess.run(cmd[:-2] + ["true"], capture_output=True, text=True, timeout=5)
                self.assertNotEqual(second.returncode, 0)
            finally:
                first.send_signal(signal.SIGTERM)
                try:
                    first.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
