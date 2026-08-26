from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ServerHealthReadonlyContractTest(unittest.TestCase):
    def test_health_is_v7_only_and_exact_paper_validated(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn('test -s "$run_root/v7_supervisor.json"', health)
        self.assertIn('test -s "$run_root/execution/runtime_status.json"', health)
        self.assertIn('[[ "$head_sha" == "$validated_sha" ]]', health)
        self.assertIn('[[ "$version" == "7" ]]', health)
        self.assertIn('[[ "$paper_only" == "true" && "$authenticated" == "false" ]]', health)
        self.assertIn('polymarket_runtime_info{adapter=\"v7\",run_root=\"paper_v7_live\",version=\"v7\"}', health)
        self.assertIn("polymarket-v7-paper", health)
        for forbidden in ("paper_v5", "paper_v6", "v5_runtime_readiness", "polymarket_v6_", "another V6"):
            self.assertNotIn(forbidden, health)

    def test_health_enforces_single_writer_and_no_legacy_processes(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn("exactly_one()", health)
        self.assertIn("scripts/paper_v7_loop.sh", health)
        self.assertIn("polymarket_trade_recorder.*paper_v7_live/execution", health)
        self.assertIn("v7_multileg_broker_runner.py", health)
        self.assertIn("v7_market_proxy.py", health)
        self.assertIn("paper_v[3-6]|scripts/v6_", health)
        self.assertIn("legacy_runtime_process_present", health)

    def test_health_checks_runtime_safety_invariants(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        for token in (
            "runtime.get('version') == 7",
            "runtime.get('paper_only') is True",
            "runtime.get('authenticated_execution') is False",
            "float(runtime.get('drawdown', 1.0)) <= 0.15 + 1e-12",
            "time.time()-float(runtime['timestamp']) <= 180",
            "allocator.get('models_expected', 0)) == 5",
            "allocator.get('models_alive', 0)) == 5",
        ):
            self.assertIn(token, health)
        self.assertIn("scripts/runtime_action_report.py", health)

    def test_macos_health_status_is_read_only(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn("bash ops/macos_service_control.sh status", health)
        self.assertNotIn("polymarket-service-control restart", health)
        self.assertNotIn("git reset --hard", health)
        self.assertNotIn("gh pr merge", health)


if __name__ == "__main__":
    unittest.main()
