from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7MacosMonitoringOwnerTest(unittest.TestCase):
    def test_stale_grafana_listener_is_drained_fail_closed(self) -> None:
        text = (ROOT / "ops/apply_v7_monitoring_config_macos.sh").read_text(encoding="utf-8")
        self.assertIn("stop_stale_grafana_listener", text)
        self.assertIn("-tiTCP:3000", text)
        self.assertIn("-sTCP:LISTEN", text)
        self.assertIn('ps -p "$pid" -o command=', text)
        self.assertIn("*grafana*|*Grafana*", text)
        self.assertIn('kill -TERM "$pid"', text)
        self.assertIn('kill -KILL "$pid"', text)
        self.assertIn("canonical Grafana port 3000 is owned by non-Grafana", text)
        self.assertNotIn("pkill", text)

    def test_dashboard_uid_is_validated_before_provisioning(self) -> None:
        text = (ROOT / "ops/apply_v7_monitoring_config_macos.sh").read_text(encoding="utf-8")
        self.assertIn("dashboard.get('uid') == expected", text)
        self.assertIn("prometheus-v7.yml", text)
        self.assertIn("grafana/provisioning/dashboards/v7.yml", text)


if __name__ == "__main__":
    unittest.main()
