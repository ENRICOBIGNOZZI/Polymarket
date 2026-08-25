#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_latest import LatestCollector  # noqa: E402
from exporter_v6 import _model_health, _multileg_fill_counts  # noqa: E402


class MonitoringV6ExporterTests(unittest.TestCase):
    def test_latest_collector_exports_stable_grafana_schema_for_v6(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            config_dir = root / "config"
            run = runs / "paper_v6_live"
            run.mkdir(parents=True)
            config_dir.mkdir(parents=True)
            config_path = config_dir / "paper_v6.json"
            config_path.write_text((ROOT / "config" / "paper_v6.json").read_text(encoding="utf-8"), encoding="utf-8")

            # runtime_status deliberately contains stale/zero relative-value fill
            # data. Grafana must still report the durable multi-leg ledger below.
            strategies = {
                "micro_maker": {
                    "equity": 1210,
                    "pnl": 10,
                    "live_units": 2,
                    "killed": False,
                    "signals": 4,
                    "best_edge": 0.0012,
                    "fills": 0,
                    "buy_fills": 0,
                    "sell_fills": 0,
                    "settle_fills": 0,
                },
                "relative_value": {
                    "equity": 5025,
                    "pnl": 25,
                    "live_units": 3,
                    "killed": False,
                },
            }
            (run / "runtime_status.json").write_text(
                json.dumps(
                    {
                        "equity": 10035,
                        "pnl": 35,
                        "drawdown": 0.02,
                        "killed": False,
                        "live_units": 5,
                        "reserved_cash": 300,
                        "gross_exposure": 255,
                        "realized_pnl": 9,
                        "execution_imbalance": 0,
                        "execution_staleness": 0,
                        "strategies": strategies,
                        "relations": {"bundles": 2, "best_edge": 0.004},
                        "graph_research": {"graph_mode":"RESEARCH_ONLY","broker_routing_enabled":False,"candidate_bundles":3,"economic_research_candidates":0,"insufficient_evidence_candidates":3,"joint_models":[{"observations":7}]},
                        "local_factor": {"bundles": 1, "clusters": 3, "reversion_tests": 11, "fdr_eligible_signals": 2, "best_edge": 0.003},
                        "external_bridge": {"materialized_signals": 0},
                    }
                ),
                encoding="utf-8",
            )
            (run / "micro_taker").mkdir()
            (run / "micro_taker" / "status.json").write_text(
                json.dumps({"exploration":{"enabled":True,"active_positions":1,"hourly_opens":2,"opened_last_tick":1,"candidate_strata_last_tick":4,"depth_rejections_last_tick":1,"realized_pnl_total":-0.25}}),
                encoding="utf-8",
            )
            (run / "v7_execution_evidence.json").write_text(
                json.dumps(
                    {
                        "schema": "polymarket_execution_evidence_v1",
                        "models": {
                            "micro_maker": {
                                "target": "short_horizon_markout",
                                "state": "INSUFFICIENT_EVIDENCE",
                                "paper_eligible": False,
                                "fills": 3,
                                "realized_pnl_observations": 2,
                                "forward_markout_observations": 0,
                                "net_pnl": -1.5,
                                "stressed_net_pnl": -2.0,
                                "bootstrap_one_sided_pvalue": 1.0,
                            },
                            "relative_value": {
                                "target": "hedged_convergence",
                                "state": "PAPER_ELIGIBLE",
                                "paper_eligible": True,
                                "fills": 7,
                                "realized_pnl_observations": 4,
                                "forward_markout_observations": 4,
                                "net_pnl": 7.0,
                                "stressed_net_pnl": 5.0,
                                "bootstrap_one_sided_pvalue": 0.02,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run / "allocator_status.json").write_text(
                json.dumps(
                    {
                        "models_alive": 4,
                        "models_expected": 5,
                        "reserve_fraction": 0.05,
                        "global_gross_fraction": 0.0255,
                        "global_max_gross_fraction": 0.45,
                        "global_max_drawdown": 0.15,
                    }
                ),
                encoding="utf-8",
            )
            with (run / "strategy_status.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["name", "expert", "alive", "status_age_seconds"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"name": "micro", "expert": "micro", "alive": 1, "status_age_seconds": 3},
                        {"name": "pca", "expert": "local_factor", "alive": 0, "status_age_seconds": 600},
                        {"name": "graph", "expert": "graph", "alive": 1, "status_age_seconds": 5},
                        {"name": "semantic", "expert": "relation_parser", "alive": 1, "status_age_seconds": 7},
                        {"name": "external", "expert": "external", "alive": 1, "status_age_seconds": 9},
                    ]
                )
            (run / "relation_guard_status.json").write_text(json.dumps({"accepted_rows": 2}), encoding="utf-8")
            with (run / "relation_intents.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["bundle_id", "market_id"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"bundle_id": "graph-bundle-1", "market_id": "m1"},
                        {"bundle_id": "graph-bundle-1", "market_id": "m2"},
                    ]
                )
            with (run / "multileg_equity.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["timestamp", "cash", "equity", "reserved_cash", "gross_entry_cash", "peak_equity", "drawdown", "killed", "live_bundles"],
                )
                writer.writeheader()
                writer.writerow({"timestamp": 1, "cash": 4900, "equity": 5025, "reserved_cash": 30, "gross_entry_cash": 150, "peak_equity": 5030, "drawdown": 0.02, "killed": 0, "live_bundles": 3})
            self._write_multileg_events(run / "multileg_events.csv")
            with (run / "bundle_ledger.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["net_pnl"])
                writer.writeheader()
                writer.writerow({"net_pnl": 7})

            collector = LatestCollector(runs, config_dir, "paper_v6_live", str(config_path), 20)
            text = collector.collect()
            self.assertIn('adapter="v6"', text)
            self.assertIn('version="v6"', text)
            self.assertIn('polymarket_model_gross_exposure_usd{expert="relative_value",model="relative_value"} 180', text)
            self.assertIn('polymarket_model_drawdown_ratio{expert="relative_value",model="relative_value"} 0.02', text)
            self.assertIn('polymarket_model_alive{expert="relative_value",model="relative_value"} 0', text)
            self.assertIn('polymarket_model_status_age_seconds{expert="relative_value",model="relative_value"} 600', text)
            self.assertIn('polymarket_model_alive{expert="micro_maker",model="micro_maker"} 1', text)
            self.assertIn('polymarket_model_fills_total{action="all",expert="relative_value",model="relative_value"} 7', text)
            self.assertIn('polymarket_model_fills_total{action="buy",expert="relative_value",model="relative_value"} 4', text)
            self.assertIn('polymarket_model_fills_total{action="sell",expert="relative_value",model="relative_value"} 2', text)
            self.assertIn('polymarket_model_fills_total{action="settle",expert="relative_value",model="relative_value"} 1', text)
            self.assertIn('polymarket_model_realized_pnl_usd{expert="relative_value",model="relative_value"} 7', text)
            self.assertIn('polymarket_model_signals_total{expert="micro_maker",model="micro_maker"} 4', text)
            self.assertIn('polymarket_model_signals_total{expert="relative_value",model="relative_value"} 1', text)
            self.assertIn('polymarket_model_best_net_edge_ratio{expert="relative_value",model="relative_value"} 0.003', text)
            self.assertIn('polymarket_allocator_models_alive 4', text)
            self.assertIn('polymarket_allocator_global_max_gross_fraction 0.45', text)
            self.assertIn('polymarket_model_execution_evidence_eligible{expert="relative_value",model="relative_value",state="PAPER_ELIGIBLE",target="hedged_convergence"} 1', text)
            self.assertIn('polymarket_model_execution_evidence_fills{expert="relative_value",model="relative_value",state="PAPER_ELIGIBLE",target="hedged_convergence"} 7', text)
            self.assertIn('polymarket_model_execution_evidence_eligible{expert="micro_maker",model="micro_maker",state="INSUFFICIENT_EVIDENCE",target="short_horizon_markout"} 0', text)
            self.assertIn('polymarket_v6_graph_research_candidate_bundles 3', text)
            self.assertIn('polymarket_v6_graph_research_joint_observations 7', text)
            self.assertIn('polymarket_v6_graph_research_broker_routing_enabled 0', text)
            self.assertIn('polymarket_v6_micro_taker_exploration_active_positions 1', text)
            self.assertIn('polymarket_v6_micro_taker_exploration_realized_pnl_usd -0.25', text)
            self.assertIn('polymarket_v6_relation_guard_accepted_rows 2', text)
            self.assertIn('polymarket_v6_relation_guard_accepted_bundles 1', text)
            self.assertIn('polymarket_v6_local_factor_candidates 11', text)
            self.assertIn('polymarket_v6_local_factor_fdr_survivors 2', text)

    @staticmethod
    def _write_multileg_events(path: Path) -> None:
        fields = ["timestamp", "event", "bundle_id", "market_id", "side", "shares", "price", "queue_ahead", "detail"]
        rows = [
            {"timestamp": 1, "event": "POST", "shares": 0},
            {"timestamp": 2, "event": "QUEUE_TRADE_DEPLETION", "shares": 0},
            {"timestamp": 3, "event": "PARTIAL_FILL", "shares": 10},
            {"timestamp": 4, "event": "PARTIAL_FILL", "shares": 5},
            {"timestamp": 5, "event": "PARTIAL_FILL", "shares": 4},
            {"timestamp": 6, "event": "PARTIAL_FILL", "shares": 3},
            {"timestamp": 7, "event": "EXIT_TAKER", "shares": 15},
            {"timestamp": 8, "event": "EXIT_TAKER", "shares": 7},
            {"timestamp": 9, "event": "SETTLE", "shares": 2},
            {"timestamp": 10, "event": "CANCEL_EFFECTIVE", "shares": 0},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def test_relative_value_fill_counter_uses_only_execution_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "multileg_events.csv"
            self._write_multileg_events(path)
            counts = _multileg_fill_counts(path)
            self.assertEqual(counts, {"fills": 7, "buy_fills": 4, "sell_fills": 2, "settle_fills": 1})

    def test_model_health_fails_closed_without_its_strategy_row(self) -> None:
        rows = {"micro": {"alive": "1", "status_age_seconds": "4"}}
        self.assertEqual(_model_health(rows, "micro_taker"), (1.0, 4.0))
        self.assertEqual(_model_health(rows, "relative_value"), (0.0, 1e12))


if __name__ == "__main__":
    unittest.main()
