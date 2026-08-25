from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ServerHealthReadonlyContractTest(unittest.TestCase):
    def test_macos_status_does_not_require_passwordless_sudo(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn(
            "sudo -n /usr/local/sbin/polymarket-service-control status", health
        )
        self.assertNotIn(
            "sudo -n launchctl print system/com.polymarket.autoupdate", health
        )
        self.assertIn('/bin/launchctl print "system/$label"', health)
        self.assertIn("state = running", health)
        for label in (
            "com.polymarket.awake",
            "com.polymarket.paper",
            "com.polymarket.exporter",
            "com.polymarket.prometheus",
            "com.polymarket.grafana",
        ):
            self.assertIn(label, health)
        self.assertIn(
            "/bin/launchctl print system/com.polymarket.autoupdate >/dev/null", health
        )

    def test_private_health_remains_fail_closed_on_runtime_invariants(self):
        health = (ROOT / ".github" / "workflows" / "server-health.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('test "$head_sha" = "$validated_sha"', health)
        self.assertIn("polymarket_allocator_models_expected 5", health)
        self.assertIn("scripts/v5_runtime_readiness.py", health)
        self.assertIn("--model-output-max-age 120", health)
        self.assertIn("--startup-grace 600", health)
        self.assertIn("scripts/runtime_action_report.py", health)
        self.assertIn('test "$status_ref" = "paper-validated"', health)
        self.assertIn('test "$status_head" = "$head_sha"', health)
        self.assertIn('test "$status_validated" = "$validated_sha"', health)

    def test_v5_staleness_alert_respects_first_output_startup_grace(self):
        exporter = (ROOT / "monitoring" / "exporter_v5.py").read_text(encoding="utf-8")
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('MODEL_OUTPUT_MAX_AGE_SECONDS = 120.0', exporter)
        self.assertIn('MODEL_STARTUP_GRACE_SECONDS = 600.0', exporter)
        self.assertIn('allocator_events.csv', exporter)
        self.assertIn('event.get("event") not in START_EVENTS', exporter)
        self.assertIn('status_age > MODEL_OUTPUT_MAX_AGE_SECONDS', exporter)
        self.assertIn('-5.0 <= start_age <= MODEL_STARTUP_GRACE_SECONDS', exporter)
        self.assertIn('alert_staleness = 0.0 if startup_grace_active else status_age', exporter)
        self.assertIn('polymarket_model_startup_grace_active', exporter)
        self.assertIn('polymarket_model_alert_staleness_seconds', exporter)
        self.assertIn('max(polymarket_model_alert_staleness_seconds) > 60', alerts)
        self.assertNotIn('max(polymarket_model_status_age_seconds) > 60', alerts)


if __name__ == "__main__":
    unittest.main()
