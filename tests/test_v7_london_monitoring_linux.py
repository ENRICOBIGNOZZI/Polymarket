from __future__ import annotations
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LondonMonitoringLinuxTests(unittest.TestCase):
    def test_bootstrap_installs_and_renders_cold_plane_but_keeps_trading_off(self):
        s = (ROOT / 'ops/v7_london_bootstrap.sh').read_text()
        self.assertIn('prometheus prometheus-node-exporter', s)
        self.assertIn('https://apt.grafana.com/gpg-full.key', s)
        self.assertIn('v7_london_install_monitoring.sh', s)
        self.assertIn('systemctl enable --now prometheus.service prometheus-node-exporter.service grafana-server.service', s)
        self.assertIn('systemctl disable --now polymarket-v7-paper.service polymarket-v7-exporter.service', s)

    def test_monitoring_installer_never_starts_services_or_touches_runtime_units(self):
        s = (ROOT / 'ops/v7_london_install_monitoring.sh').read_text()
        self.assertNotIn('enable --now', s)
        self.assertNotIn('systemctl start', s)
        self.assertNotIn('polymarket-v7-paper.service', s)
        self.assertNotIn('polymarket-v7-exporter.service', s)
        self.assertIn('prometheus.service.d/polymarket-v7.conf', s)
        self.assertIn('grafana-server.service.d/polymarket-v7.conf', s)
        self.assertIn('existing monitoring bundle differs for exact SHA', s)
        self.assertIn('POLYMARKET_SYSTEMD_ROOT', s)
        self.assertIn('POLYMARKET_SYSTEMCTL', s)

    def test_cutover_validates_cold_plane_before_starting_paper(self):
        s = (ROOT / 'ops/v7_london_cutover.sh').read_text()
        install = s.index('v7_london_install_monitoring.sh')
        grafana_health = s.index('127.0.0.1:3000/api/health', install)
        paper_start = s.index('systemctl enable --now polymarket-v7-paper.service', grafana_health)
        prom_scrape = s.index('up{job="polymarket-v7"}', paper_start)
        self.assertLess(install, grafana_health)
        self.assertLess(grafana_health, paper_start)
        self.assertLess(paper_start, prom_scrape)

    def test_monitoring_files_are_runtime_support_only(self):
        manifest = (ROOT / 'deploy/london/runtime_manifest.json').read_text()
        self.assertIn('ops/v7_london_monitoring_bundle.py', manifest)
        self.assertIn('ops/v7_london_install_monitoring.sh', manifest)
        policy = (ROOT / 'config/v7_native_critical_path_policy.json').read_text()
        self.assertNotIn('v7_london_install_monitoring', policy)
        self.assertNotIn('grafana', policy.lower())
        self.assertNotIn('prometheus', policy.lower())


if __name__ == '__main__':
    unittest.main()
