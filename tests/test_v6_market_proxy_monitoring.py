#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V6MarketProxyMonitoringContractTests(unittest.TestCase):
    def test_prometheus_has_fail_closed_proxy_health_alerts(self) -> None:
        alerts = (ROOT / "monitoring" / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
        for alert in (
            "PolymarketV6MarketProxyMissingOrStale",
            "PolymarketV6MarketProxyGammaUnavailable",
            "PolymarketV6MarketProxyStaleCache",
            "PolymarketV6MarketProxyUnavailable",
        ):
            self.assertIn(alert, alerts)
        self.assertIn("polymarket_v6_market_proxy_status_age_seconds > 120", alerts)
        self.assertIn('polymarket_v6_market_proxy_info{source="stale_cache"}', alerts)
        self.assertIn('polymarket_v6_market_proxy_info{source="unavailable"}', alerts)

    def test_grafana_data_health_dashboard_exposes_proxy_provenance(self) -> None:
        dashboard = json.loads(
            (ROOT / "monitoring" / "grafana" / "dashboards" / "polymarket-data-health.json").read_text(
                encoding="utf-8"
            )
        )
        expressions = {
            target.get("expr")
            for panel in dashboard.get("panels", [])
            for target in panel.get("targets", [])
            if isinstance(target, dict)
        }
        for metric in (
            "polymarket_v6_market_proxy_info",
            "polymarket_v6_market_proxy_status_age_seconds",
            "polymarket_v6_market_proxy_upstream_gamma_ok",
            "polymarket_v6_market_proxy_cache_age_seconds",
            "polymarket_v6_market_proxy_failures_total",
            "polymarket_v6_market_proxy_markets",
        ):
            self.assertIn(metric, expressions)


if __name__ == "__main__":
    unittest.main()
