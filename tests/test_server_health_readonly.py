from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ServerHealthReadonlyContractTest(unittest.TestCase):
    def test_macos_status_does_not_require_passwordless_sudo(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertNotIn("sudo -n /usr/local/sbin/polymarket-service-control status", health)
        self.assertNotIn("sudo -n launchctl print system/com.polymarket.autoupdate", health)
        self.assertIn('/bin/launchctl print "system/$label"', health)
        self.assertIn("state = running", health)
        for label in ("com.polymarket.awake", "com.polymarket.paper", "com.polymarket.exporter", "com.polymarket.prometheus", "com.polymarket.grafana"):
            self.assertIn(label, health)
        self.assertIn("/bin/launchctl print system/com.polymarket.autoupdate >/dev/null", health)

    def test_private_health_is_v7_only_and_fail_closed(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn('test "$head_sha" = "$validated_sha"', health)
        self.assertIn("polymarket_v7_runtime_info", health)
        self.assertIn("polymarket_allocator_models_expected 5", health)
        self.assertIn("polymarket_allocator_models_alive 5", health)
        self.assertIn("polymarket_v7_runtime_status_v1", health)
        self.assertIn("polymarket_v7_market_proxy_status_v1", health)
        self.assertIn("scripts/paper_v7_loop.sh", health)
        self.assertIn('test "$status_ref" = "paper-validated"', health)
        self.assertIn('test "$status_head" = "$head_sha"', health)
        self.assertIn('test "$status_validated" = "$validated_sha"', health)
        for token in ('paper_v4', 'paper_v5', 'paper_v6', 'scripts/v4_', 'scripts/v5_', 'scripts/v6_', 'polymarket_v6_', 'another V6'):
            self.assertNotIn(token, health)

    def test_private_health_fails_closed_on_sustained_recorder_http_failure(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        for token in ("recorder_tail_lines=", "recorder_success_ticks=", "recorder_http_failures=", "recorder_proxy_failures=", "recorder_data_failures=", "Gamma markets HTTP 503"):
            self.assertIn(token, health)
        self.assertIn('"$recorder_tail_lines" -ge 5', health)
        self.assertIn('"$recorder_success_ticks" -eq 0', health)
        self.assertIn('"$recorder_data_failures" -eq "$recorder_tail_lines"', health)
        self.assertIn("private recorder data path unhealthy", health)

    def test_private_health_fails_closed_on_unrecovered_state_integrity_errors(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn("latest_runtime_failure()", health)
        self.assertIn("fatal: Cannot replace state file:", health)
        self.assertIn("another V7 multi-leg broker already owns", health)
        self.assertIn("FileNotFoundError:", health)
        self.assertIn("private runtime state integrity unhealthy: multileg", health)
        self.assertIn("private runtime state integrity unhealthy: hard_arb", health)
        self.assertIn("private runtime state integrity unhealthy: micro_taker", health)
        self.assertIn('"$last_failure" -ge "$last_success"', health)


if __name__ == "__main__":
    unittest.main()
