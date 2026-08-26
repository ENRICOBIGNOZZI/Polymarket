from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ServerHealthDiagnosticsContractTest(unittest.TestCase):
    def test_exporter_health_failure_is_captured_before_fail_closed_exit(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertNotIn("curl -fsS http://127.0.0.1:9108/healthz >/dev/null", health)
        self.assertIn("exporter_healthz_http=%s", health)
        self.assertIn("exporter_healthz_body_begin", health)
        self.assertIn("exporter_healthz_body_end", health)
        self.assertIn("exporter_healthz_failed=1", health)
        self.assertIn("private exporter healthz unhealthy: HTTP", health)
        self.assertIn("trade_recorder_tail_begin", health)
        self.assertIn("multileg_tail_begin", health)
        self.assertLess(health.index("exporter_healthz_body_begin"), health.index("trade_recorder_tail_begin"))
        self.assertLess(health.index("trade_recorder_tail_begin"), health.index("private exporter healthz unhealthy: HTTP"))

    def test_healthz_probe_does_not_turn_503_into_success(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn('[[ "$exporter_healthz_code" == "200" ]] || exporter_healthz_failed=1', health)
        self.assertIn('if [[ "$exporter_healthz_failed" -ne 0 ]]; then', health)

    def test_diagnostics_are_v7_only(self) -> None:
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn("polymarket_v7_runtime_status_v1", health)
        self.assertIn("polymarket_v7_market_proxy_status_v1", health)
        for token in ('paper_v4', 'paper_v5', 'paper_v6', 'polymarket_v6_', 'another V6'):
            self.assertNotIn(token, health)


if __name__ == "__main__":
    unittest.main()
