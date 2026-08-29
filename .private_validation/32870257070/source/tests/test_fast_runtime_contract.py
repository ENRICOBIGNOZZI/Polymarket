from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
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
        self.assertLess(selector.index("runtime_singleton_launcher.py"), selector.index("start_champion()"))
        self.assertIn('tmp="$(mktemp "$path.tmp.XXXXXX")"', selector)

    def test_v6_status_atomic_writers_use_pid_unique_temp_paths(self) -> None:
        status = (ROOT / "scripts" / "v6_runtime_status.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(status.count('path.name + f".tmp.{os.getpid()}"'), 2)
        self.assertNotIn('with_suffix(path.suffix + ".tmp")', status)

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
