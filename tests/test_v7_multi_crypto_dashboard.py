from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "monitoring/grafana/dashboards/polymarket-v7-multi-crypto.json"


class MultiCryptoDashboardTest(unittest.TestCase):
    def test_dashboard_is_dedicated_multi_crypto_performance_surface(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        self.assertEqual(dashboard["uid"], "polymarket-v7-multi-crypto")
        self.assertIn("Multi-Crypto Performance", dashboard["title"])
        text = json.dumps(dashboard)
        for metric in (
            "polymarket_mc_risk_equity_lower_bound_usd", "polymarket_mc_pnl_lower_bound_usd",
            "polymarket_mc_realized_pnl_usd", "polymarket_mc_lane_realized_pnl_usd",
            "polymarket_mc_strategy_realized_pnl_usd", "polymarket_mc_attribution_status_code",
            "polymarket_mc_attribution_gap_usd", "polymarket_mc_lane_economic_evidence_present",
            "polymarket_mc_lane_realized_return_on_turnover", "polymarket_mc_lane_fees_bps",
            "polymarket_mc_lane_open_cost_at_risk_usd", "polymarket_v7_native_active_workers",
            "polymarket_v7_native_target_contexts", "polymarket_v7_native_missing_contexts",
            "polymarket_v7_native_observations_published", "polymarket_v7_native_observations_written",
            "polymarket_v7_native_observations_dropped", "polymarket_v7_native_observations_queue_depth",
            "polymarket_v7_book_data_runtime_ready", "polymarket_v7_external_data_ready_assets",
            "polymarket_v7_native_context_decision_reason_total",
        ):
            self.assertIn(metric, text)
        self.assertNotIn("polymarket_mc_coordinator_candidate_", text)
        self.assertNotIn("polymarket_mc_shadow_", text)
        self.assertIn("N/A", text)
        self.assertNotIn("polymarket_runtime_pnl_usd", text)
        self.assertNotIn("polymarket_runtime_equity_usd", text)
        self.assertNotIn("polymarket_mc_portfolio_equity_usd", text)
        self.assertNotIn("polymarket_mc_total_pnl_usd", text)
        self.assertNotIn("polymarket_mc_unrealized_pnl_usd", text)
        self.assertNotIn("$instance", text)

    def test_native_decision_funnel_is_visible_by_context(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        titles = {p.get("title") for p in dashboard["panels"]}
        self.assertIn("DECISION FUNNEL", titles)
        self.assertIn("Decision Reasons", titles)
        self.assertIn("Reject Funnel by Lane", titles)
        self.assertIn("Key Economic Rejections by Lane", titles)
        expressions = "\n".join(
            target.get("expr", "")
            for panel in dashboard["panels"]
            for target in panel.get("targets", [])
        )
        self.assertIn('reason="MARKET_ALREADY_REPRICED"', expressions)
        self.assertIn('reason="NET_EDGE_NON_POSITIVE"', expressions)
        self.assertIn('asset=~"$asset"', expressions)
        self.assertIn('horizon=~"$horizon"', expressions)

    def test_equity_is_not_mixed_into_pnl_timeseries(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        pnl_panel = next(p for p in dashboard["panels"] if p.get("title") == "Verified PnL Bounds")
        expressions = [target.get("expr", "") for target in pnl_panel.get("targets", [])]
        self.assertTrue(all("pnl" in expr for expr in expressions))
        self.assertTrue(all("equity" not in expr for expr in expressions))

    def test_blank_legacy_instance_url_cannot_hide_london_data(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        variables = {row["name"]: row for row in dashboard["templating"]["list"]}
        self.assertNotIn("instance", variables)
        text = json.dumps(dashboard)
        self.assertNotIn('instance="$instance"', text)
        expressions = [target.get("expr", "") for panel in dashboard["panels"] for target in panel.get("targets", [])]
        self.assertTrue(any('instance=~".+"' in expr for expr in expressions))

    def test_asset_horizon_filters_cover_full_requested_universe(self) -> None:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        variables = {row["name"]: row for row in dashboard["templating"]["list"]}
        self.assertEqual(variables["asset"]["query"], "BTC,ETH,SOL,XRP,DOGE,BNB")
        self.assertEqual(variables["horizon"]["query"], "M5,M15,H1,H4,D1")
        self.assertTrue(variables["asset"]["includeAll"])
        self.assertTrue(variables["horizon"]["includeAll"])

    def test_manifest_and_control_room_link_the_dashboard(self) -> None:
        manifest = json.loads((ROOT / "monitoring/v7_monitoring_manifest.json").read_text())
        self.assertEqual(manifest["grafana"]["multi_crypto_dashboard"],
                         "monitoring/grafana/dashboards/polymarket-v7-multi-crypto.json")
        home = json.loads((ROOT / "monitoring/grafana/dashboards/polymarket-v7.json").read_text())
        self.assertTrue(any(link.get("title") == "Multi-Crypto Performance" for link in home.get("links", [])))


if __name__ == "__main__":
    unittest.main()
