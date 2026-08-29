from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ServerHealthDiagnosticsContractTest(unittest.TestCase):
    def test_exporter_health_body_is_captured_before_fail_closed_exit(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")

        self.assertNotIn("curl -fsS http://127.0.0.1:9108/healthz >/dev/null", health)
        self.assertIn("exporter_healthz_http=%s", health)
        self.assertIn("exporter_healthz_body_begin", health)
        self.assertIn("exporter_healthz_body_end", health)
        self.assertIn('test "$exporter_healthz_code" = "200"', health)

        body_pos = health.index("exporter_healthz_body_begin")
        fail_pos = health.index('test "$exporter_healthz_code" = "200"')
        self.assertLess(body_pos, fail_pos)

    def test_generic_runtime_diagnostics_follow_contract_selected_state_root(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/runtime_contract_health.py", health)
        self.assertIn("--json", health)
        self.assertIn('print(json.load(sys.stdin)["state_root"])', health)
        self.assertIn("for log in trade_recorder.log multileg.log maker.log micro_taker.log hard_arb.log; do", health)
        self.assertIn("printf '%s_tail_begin", health)
        self.assertIn("printf '%s_tail_end", health)
        self.assertNotIn("runs/paper_v6_live", health)

    def test_healthz_probe_does_not_turn_503_into_success(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(encoding="utf-8")
        self.assertIn("--write-out '%{http_code}'", health)
        self.assertIn('test "$exporter_healthz_code" = "200"', health)
        self.assertNotIn("exporter_healthz_failed=0 # ignore", health)


if __name__ == "__main__":
    unittest.main()
