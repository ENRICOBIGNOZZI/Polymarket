from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ServerHealthDiagnosticsContractTest(unittest.TestCase):
    def test_exporter_health_failure_is_captured_before_fail_closed_exit(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")

        self.assertNotIn("curl -fsS http://127.0.0.1:9108/healthz >/dev/null", health)
        self.assertIn("exporter_healthz_http=%s", health)
        self.assertIn("exporter_healthz_body_begin", health)
        self.assertIn("exporter_healthz_body_end", health)
        self.assertIn("exporter_healthz_failed=1", health)
        self.assertIn("private exporter healthz unhealthy: HTTP", health)
        self.assertIn("trade_recorder_tail_begin", health)
        self.assertIn("multileg_tail_begin", health)

        body_pos = health.index("exporter_healthz_body_begin")
        recorder_pos = health.index("trade_recorder_tail_begin")
        final_fail_pos = health.index("private exporter healthz unhealthy: HTTP")
        self.assertLess(body_pos, recorder_pos)
        self.assertLess(recorder_pos, final_fail_pos)

    def test_healthz_probe_does_not_turn_503_into_success(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn('if [[ "$exporter_healthz_code" != "200" ]]; then', health)
        self.assertIn('if [[ "$exporter_healthz_failed" -ne 0 ]]; then', health)
        self.assertNotIn("exporter_healthz_failed=0 # ignore", health)


if __name__ == "__main__":
    unittest.main()
