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

    def test_v7_monitoring_publishes_only_private_tailscale_serve_route(self) -> None:
        text = (ROOT / "ops/apply_v7_monitoring_config_macos.sh").read_text(encoding="utf-8")
        self.assertIn("mamma-portfolio.tail1bae85.ts.net", text)
        self.assertIn('set --hostname="$TAILSCALE_HOSTNAME"', text)
        self.assertIn("GRAFANA_URL=\"${POLYMARKET_GRAFANA_URL:-https://${TAILSCALE_FQDN}}\"", text)
        self.assertIn("serve --bg --https=443 localhost:3000", text)
        self.assertIn("serve --bg --http=80 localhost:3000", text)
        self.assertIn("serve status", text)
        self.assertIn("polymarket-v7-canonical-paper-economics", text)
        self.assertNotIn("funnel", text.lower())

    def test_external_macos_monitoring_commands_are_bounded(self) -> None:
        text = (ROOT / "ops/apply_v7_monitoring_config_macos.sh").read_text(encoding="utf-8")
        self.assertIn('POLYMARKET_MONITORING_COMMAND_TIMEOUT_SECONDS:-15', text)
        self.assertIn("run_bounded()", text)
        self.assertIn("start_new_session=True", text)
        self.assertIn("os.killpg(process.pid, signal.SIGTERM)", text)
        self.assertIn("os.killpg(process.pid, signal.SIGKILL)", text)
        self.assertIn('run_bounded "$binary" "$@"', text)
        self.assertIn('run_bounded "$ts" status --json', text)
        self.assertIn('run_bounded "$ts" serve status', text)
        self.assertIn("shlex.join(command)", text)
        status = text.index('run_bounded "$ts" status --json')
        mutate = text.index('tailscale_admin "$ts" set --hostname=')
        self.assertLess(status, mutate)
        serve_status = text.index('serve_status="$(run_bounded "$ts" serve status')
        serve_mutate = text.index('tailscale_admin "$ts" serve --bg --https=443')
        self.assertLess(serve_status, serve_mutate)
        self.assertIn('https_route="https://${TAILSCALE_FQDN}"', text)
        self.assertIn('http_route="http://${TAILSCALE_FQDN}"', text)
        self.assertGreaterEqual(text.count('grep -Fq "$https_route" <<<"$serve_status"'), 2)
        self.assertGreaterEqual(text.count('grep -Fq "$http_route" <<<"$serve_status"'), 2)
        self.assertGreaterEqual(text.count('grep -Fq "localhost:3000" <<<"$serve_status"'), 2)

    def test_server_health_checks_operator_route_and_trade_funnel(self) -> None:
        text = (ROOT / ".github" / "workflows" / "v7-paper-server-health.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("GRAFANA_HOSTNAME: mamma-portfolio", text)
        self.assertIn("GRAFANA_FQDN: mamma-portfolio.tail1bae85.ts.net", text)
        self.assertIn("GRAFANA_URL: https://mamma-portfolio.tail1bae85.ts.net", text)
        self.assertIn('tailscale ping --until-direct=false --c 1 --timeout=10s "$GRAFANA_HOSTNAME"', text)
        self.assertIn('getent hosts "$GRAFANA_FQDN"', text)
        self.assertIn('$GRAFANA_URL/api/health', text)
        self.assertIn('$GRAFANA_URL/api/dashboards/uid/polymarket-v7', text)
        for metric in (
            "polymarket_execution_opportunities",
            "polymarket_execution_orders_submitted",
            "polymarket_execution_fills",
            "polymarket_execution_complete_fills",
            "polymarket_execution_final_pnl_usd",
            "polymarket_runtime_pnl_usd",
            "polymarket_v7_canonical_submitted_units",
            "polymarket_v7_canonical_complete_units",
        ):
            self.assertIn(metric, text)


if __name__ == "__main__":
    unittest.main()
