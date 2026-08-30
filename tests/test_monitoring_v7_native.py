from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "monitoring" / "exporter_v7.py"
SPEC = importlib.util.spec_from_file_location("exporter_v7", EXPORTER_PATH)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exporter
SPEC.loader.exec_module(exporter)


class V7NativeMonitoringTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def _sha() -> str:
        return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()

    def _fixture(self, root: Path, *, now: int = 1_000) -> None:
        sha = self._sha()
        family_rows = {}
        for family in ("sports_latency", "cross_platform", "wallet_intelligence"):
            wallet = family == "wallet_intelligence"
            cross = family == "cross_platform"
            family_rows[family] = {
                "authority": "RESEARCH", "paper_only": True,
                "authenticated_execution": False, "real_order_submission": False,
                "process_state": "RUNNING",
                "evidence_state": "BLOCKED_CONFIG" if wallet else "BLOCKED_EXTERNAL",
                "last_attempt_ts": 0 if wallet else now - 2,
                "last_success_ts": now - 2 if cross else 0,
                "execution_authority": False, "capital_authority": False,
                "oms_authority": False, "ledger_write_authority": False,
                "promotion_authority": False,
                "status_path": str(root / "shadow" / family / "status.json"),
                "output_path": str(root / "shadow" / family),
                "implementation_complete": not wallet,
                "feed_status": "NOT_CONFIGURED" if wallet else ("OPERATIONAL" if cross else "CREDENTIALS_REQUIRED"),
                "feed_operational": cross,
                "mapping_status": "NOT_CONFIGURED" if wallet else ("NO_VERIFIED_EQUIVALENCE" if cross else "NO_VERIFIED_MAPPING"),
                "verified_mappings": 0, "forward_collection_active": False,
                "blocker": "" if wallet else ("BLOCKED_NO_VERIFIED_EQUIVALENCE" if cross else "BLOCKED_PROVIDER_CREDENTIALS:PM_V7_SPORTRADAR_API_KEY"),
            }
        self._write(
            root / "control" / "runtime_status.json",
            {
                "schema": "polymarket_v7_runtime_status_v2",
                "timestamp": now - 5,
                "version": 7,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "model_sha": sha,
                "config_hash": "config-hash",
                "policy_hash": "policy-hash",
                "model_hash": "model-hash",
                "run_id": "run-id",
                "ledger_id": "ledger-id",
                "server_id": "server-id",
                "pid": os.getpid(),
                "state": "running",
                "killed": False,
            },
        )
        self._write(
            root / "control" / "research_sleeves_manifest.json",
            {
                "schema": "polymarket_v7_research_sleeves_manifest_v1",
                "version": 7,
                "model_sha": sha,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "supervisor_pid": os.getpid(),
                "timestamp": now - 2,
                "families": family_rows,
            },
        )
        for family in ("sports_latency", "cross_platform", "wallet_intelligence"):
            self._write(
                root / "shadow" / family / "status.json",
                {
                    "schema": "polymarket_v7_research_shadow_status_v1",
                    "version": 7,
                    "family": family,
                    "model_sha": sha,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "process_state": "RUNNING",
                    **family_rows[family],
                    "timestamp": now - 2,
                },
            )
        budgets = {"graph_rv": 3400.0, "hard_arb": 2200.0, "micro_taker": 1200.0, "micro_maker": 2200.0, "external": 800.0, "reserve": 200.0}
        self._write(
            root / "control" / "allocations" / "manifest.json",
            {
                "schema": "polymarket_v7_capital_allocation_v1",
                "paper_only": True,
                "authenticated_execution": False,
                "account_starting_capital": 10_000.0,
                "budgets": budgets,
            },
        )
        self._write(
            root / "control" / "portfolio_state.json",
            {
                "schema": "polymarket_v7_portfolio_guard_v1",
                "timestamp": now - 3,
                "paper_only": True,
                "authenticated_execution": False,
                "account_starting_capital": 10_000.0,
                "equity": 10_050.0,
                "peak": 10_100.0,
                "drawdown": 50.0 / 10_100.0,
                "max_drawdown": 0.15,
                "killed": False,
                "sleeves": {
                    "graph_rv": {"budget": 3400.0, "equity": 3420.0, "source": "reported", "killed": False},
                    "hard_arb": {"budget": 2200.0, "equity": 2210.0, "source": "reported", "killed": False},
                    "micro_taker": {"budget": 1200.0, "equity": 1220.0, "source": "reported", "killed": False},
                    "micro_maker": {"budget": 2200.0, "equity": 2200.0, "source": "inactive_reserved", "killed": False},
                    "external": {"budget": 800.0, "equity": 800.0, "source": "inactive_reserved", "killed": False},
                    "reserve": {"budget": 200.0, "equity": 200.0, "source": "reserve", "killed": False},
                },
            },
        )
        self._write(
            root / "graph_rv" / "status.json",
            {
                "schema": "polymarket_v7_graph_rv_status_v1",
                "timestamp": now - 5,
                "paper_only": True,
                "authenticated_execution": False,
                "model_sha": sha,
                "cash": 3300.0,
                "equity": 3420.0,
                "drawdown": 0.01,
                "killed": False,
                "bundle_states": {},
            },
        )
        self._write(root / "graph_rv" / "scan_status.json", {"timestamp": now - 7, "paper_only": True, "bundles": 2})
        self._write(root / "graph_rv" / "state.json", {"model_sha": sha, "bundles": {}})
        self._write(
            root / "hard_arb" / "status.json",
            {"timestamp": now - 5, "paper_only": True, "authenticated_execution": False, "cash": 2210.0, "equity_cost_basis": 2210.0, "realized_pnl_total": 10.0, "candidates": 1, "killed": False},
        )
        self._write(
            root / "micro_taker" / "status.json",
            {"timestamp": now - 5, "paper_only": True, "authenticated_execution": False, "cash": 1180.0, "equity": 1220.0, "realized_pnl_total": 20.0, "signals": 3, "best_edge": 0.002, "open_positions": 1, "killed": False},
        )
        self._write(root / "micro_maker" / "status.json", {
            "schema": "polymarket_v7_professional_maker_status_v1",
            "timestamp_ms": (now - 5) * 1000,
            "model_sha": sha,
            "paper_only": True,
            "authenticated_execution": False,
            "killed": False,
            "source": "full_visible_bid_depth_net_verified_fee_and_slippage",
        })
        self._write(root / "micro_maker" / "runtime_diagnostics.json", {
            "schema": "polymarket_v7_maker_runtime_diagnostics_v1",
            "timestamp_ms": (now - 5) * 1000,
            "model_sha": sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "feed_workers": 5,
            "feed_connected_workers": 5,
            "feed_messages": 1234,
            "feed_reconnects": 0,
            "feed_errors": 0,
            "decisions": 4321,
            "quote_intents": 7,
            "rejected_nonpositive_robust_ev": 4000,
            "rejected_positive_point_ev": 3500,
            "best_rejected_robust_ev_per_share": -0.0001,
            "best_rejected_point_ev_per_share": 0.00072,
            "reason_counts": {"NO_ECONOMIC_QUOTE": 4000, "QUOTE": 7},
        })
        self._write(root / "micro_maker" / "selector_status.json", {
            "schema": "polymarket_v7_maker_selector_status_v1",
            "timestamp_ms": (now - 5) * 1000,
            "model_sha": sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "OPERATIONAL_RECENT_FLOW",
            "runtime_selection_pinned": True,
            "candidate_rotation_pending": True,
            "candidate_selected_count": 40,
            "ready": True,
            "degraded": False,
            "source": "adaptive_universe_recent_flow",
            "selected_count": 40,
        })
        self._write(root / "micro_maker" / "rotation_status.json", {
            "schema": "polymarket_v7_maker_cohort_rotation_status_v1",
            "timestamp_ms": (now - 5) * 1000,
            "model_sha": sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "RUNNING",
            "rotation_count": 3,
            "runtime_membership_sha256": "runtime-membership",
            "candidate_membership_sha256": "candidate-membership",
        })
        self._write(root / "external" / "status.json", {"timestamp": now - 5, "paper_only": True, "authenticated_execution": False})
        self._write(root / "osint" / "status.json", {"schema": "polymarket_v7_osint_collector_status_v1", "timestamp_ms": (now - 5) * 1000, "paper_only": True, "authenticated_execution": False, "real_order_submission": False, "enabled_sources": 3, "healthy_sources": 3})
        self._write(root / "osint" / "mapping_status.json", {"schema": "polymarket_v7_osint_mapping_status_v1", "version": 7, "family": "osint", "model_sha": sha, "timestamp_ms": (now - 5) * 1000, "paper_only": True, "research_only": True, "authenticated_execution": False, "real_order_submission": False, "implementation_complete": True, "mapping_pipeline": True, "title_similarity_verification_forbidden": True, "verified_mappings": 0, "candidate_mappings": 2, "forward_collection_active": False})
        self._write(root / "market_open" / "status.json", {"schema": "polymarket_v7_market_open_collector_status_v1", "timestamp_ms": (now - 5) * 1000, "paper_only": True, "authenticated_execution": False, "real_order_submission": False, "observed_markets": 10})
        self._write(root / "universe" / "status.json", {
            "schema": "polymarket_v7_adaptive_universe_status_v1", "version": 7,
            "timestamp_ms": (now - 5) * 1000, "model_sha": sha, "state": "OPERATIONAL",
            "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
            "execution_authority": False, "discovery_exhaustive": True,
            "pagination_loop_guard_hit": False, "discovered_markets": 2000,
            "eligible_markets": 1800, "skipped_markets": 200,
            "skipped_by_reason": {"BELOW_MINIMUM_LIQUIDITY": 200},
            "tier_counts": {"HOT": 400, "WARM": 1250, "COLD": 150},
            "resource_capacities": {"hot_limiting_dimensions": ["cpu"], "warm_limiting_dimensions": ["scan_time"]},
            "pages": 4, "scan_duration_ms": 125.0,
        })
        self._write(
            root / "canonical_economics.json",
            {
                "schema": "polymarket_v7_canonical_economics_v1",
                "expected_model_sha": sha,
                "paper_only": True,
                "authenticated_execution": False,
                "state": "MORE_EVIDENCE_REQUIRED",
                "promotion_ready": False,
                "submitted_units": 2,
                "complete_units": 1,
                "net_pnl": 7.0,
                "stressed_net_pnl": {"1.0": 7.0, "1.5": 4.0, "2.0": 1.0},
                "reason_codes": ["sample_not_mature"],
            },
        )
        ledger = root / "ledger" / "execution.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("", encoding="utf-8")
        tape = root / "trade_tape.csv"
        tape.write_text(
            "timestamp,received_ms,market_id,asset_id,side,price,size,trade_id\n"
            f"{now - 1},{(now - 1) * 1000},m,t,SELL,0.5,2,tr1\n",
            encoding="utf-8",
        )
        latency = root / "micro_maker" / "latency.csv"
        latency.write_text(
            "recorded_ts_ms,record_kind,market_handle,instrument_handle,state_version,parse_ns,book_ns,feature_ns,decision_ns,risk_ns,tx_queue_ns,execution_ns,receive_to_intent_ns\n"
            "999000,candidate,1,2,3,1000,2000,3000,4000,5000,0,0,15000\n"
            "999001,paper_execution,1,2,3,1000,2000,3000,4000,5000,6000,7000,15000\n",
            encoding="utf-8",
        )

    def test_healthy_v7_fixture_exports_canonical_runtime_and_economics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertEqual(exporter.health_reasons(snapshot), [])
            metrics = exporter.render_prometheus(snapshot)
            self.assertIn("polymarket_v7_runtime_info 1", metrics)
            self.assertIn("polymarket_v7_runtime_identity_info", metrics)
            self.assertIn('polymarket_runtime_info{adapter="v7_native",run_root="paper_v7_live",version="v7"} 1', metrics)
            self.assertIn("polymarket_v7_operator_authority_valid 1", metrics)
            self.assertIn("polymarket_v7_authority_max_drawdown_ratio 0.15", metrics)
            self.assertIn("polymarket_v7_paper_only_contract_ok 1", metrics)
            self.assertIn("polymarket_v7_authenticated_execution_disabled 1", metrics)
            self.assertIn('polymarket_v7_component_ready{component="professional_maker"} 1', metrics)
            self.assertIn("polymarket_v7_maker_selector_ready 1", metrics)
            self.assertIn("polymarket_v7_maker_selector_fallback_active 0", metrics)
            self.assertIn("polymarket_v7_maker_runtime_selection_pinned 1", metrics)
            self.assertIn("polymarket_v7_maker_candidate_rotation_pending 1", metrics)
            self.assertIn("polymarket_v7_maker_candidate_selected_markets 40", metrics)
            self.assertIn("polymarket_v7_maker_cohort_supervisor_ready 1", metrics)
            self.assertIn("polymarket_v7_maker_cohort_rotations_total 3", metrics)
            self.assertIn("polymarket_v7_maker_rotation_draining 0", metrics)
            self.assertIn("polymarket_v7_maker_rotation_blocked_nonflat 0", metrics)
            self.assertIn("polymarket_v7_maker_feed_connected_workers 5", metrics)
            self.assertIn("polymarket_v7_maker_feed_messages_total 1234", metrics)
            self.assertIn("polymarket_v7_maker_decisions_total 4321", metrics)
            self.assertIn("polymarket_v7_maker_quote_intents_total 7", metrics)
            self.assertIn("polymarket_v7_maker_rejected_positive_point_ev_total 3500", metrics)
            self.assertIn("polymarket_v7_maker_best_rejected_point_ev_per_share 0.00072", metrics)
            self.assertIn('polymarket_v7_maker_decision_reason_total{reason="NO_ECONOMIC_QUOTE"} 4000', metrics)
            self.assertIn("polymarket_v7_live_model_target_count 12", metrics)
            self.assertIn("polymarket_v7_live_model_operational_count 8", metrics)
            self.assertIn("polymarket_v7_live_model_blocked_count 4", metrics)
            self.assertIn("polymarket_v7_live_model_blocked_config_count 1", metrics)
            self.assertIn("polymarket_v7_live_model_blocked_external_count 3", metrics)
            self.assertIn("polymarket_v7_live_model_scope_wired 1", metrics)
            self.assertIn("polymarket_v7_live_model_target_operational 0", metrics)
            self.assertIn("polymarket_v7_trade_tape_rows 1", metrics)
            self.assertIn("polymarket_v7_canonical_submitted_units 2", metrics)
            self.assertIn("polymarket_v7_canonical_complete_units 1", metrics)
            self.assertIn("polymarket_runtime_realized_pnl_usd 7", metrics)
            self.assertIn("polymarket_v7_latency_samples_present 1", metrics)
            self.assertIn("polymarket_v7_universe_discovery_exhaustive 1", metrics)
            self.assertIn('polymarket_v7_universe_tier_markets{tier="HOT"} 400', metrics)
            self.assertIn('polymarket_v7_universe_tier_markets{tier="WARM"} 1250', metrics)
            self.assertIn('polymarket_v7_universe_tier_markets{tier="COLD"} 150', metrics)
            self.assertIn('polymarket_v7_latency_stage_nanoseconds{stage="parse_ns",percentile="p99"} 1000', metrics)
            self.assertIn('polymarket_v7_latency_stage_nanoseconds{stage="tx_queue_ns",percentile="p99"} 6000', metrics)
            self.assertNotIn("polymarket_v7_shadow_alive", metrics)
            self.assertNotIn("polymarket_v7_market_proxy_markets", metrics)

    def test_stale_trade_tape_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root, now=1_000)
            tape = run_root / "trade_tape.csv"
            tape.write_text("timestamp,received_ms,market_id,asset_id,side,price,size,trade_id\n700,700000,m,t,SELL,0.5,2,tr1\n", encoding="utf-8")
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("trade_tape_stale", exporter.health_reasons(snapshot))

    def test_empty_trade_tape_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            (run_root / "trade_tape.csv").write_text("timestamp,received_ms,market_id,asset_id,side,price,size,trade_id\n", encoding="utf-8")
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("trade_tape_empty", exporter.health_reasons(snapshot))

    def test_authenticated_runtime_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "control" / "runtime_status.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["authenticated_execution"] = True
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("authenticated_execution_not_disabled", exporter.health_reasons(snapshot))

    def test_research_shadow_cannot_claim_active_without_adapter_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "control" / "research_sleeves_manifest.json"
            value = json.loads(path.read_text())
            value["families"]["sports_latency"]["evidence_state"] = "ACTIVE"
            value["families"]["sports_latency"]["last_success_ts"] = 999
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn(
                "research_sleeve_false_active:sports_latency",
                exporter.health_reasons(snapshot),
            )
            metrics = exporter.render_prometheus(snapshot)
            self.assertIn("polymarket_v7_live_model_scope_wired 0", metrics)
            self.assertIn("polymarket_v7_live_model_target_operational 0", metrics)

    def test_research_manifest_and_collectors_are_fresh_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            manifest_path = run_root / "control" / "research_sleeves_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["timestamp"] = 1
            self._write(manifest_path, manifest)
            osint_path = run_root / "osint" / "status.json"
            osint = json.loads(osint_path.read_text())
            osint["real_order_submission"] = True
            self._write(osint_path, osint)
            market_path = run_root / "market_open" / "status.json"
            market = json.loads(market_path.read_text())
            market["timestamp_ms"] = 1
            self._write(market_path, market)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            reasons = exporter.health_reasons(snapshot)
            self.assertIn("research_sleeves_manifest_stale", reasons)
            self.assertIn("osint_live_collector_missing_or_unsafe", reasons)
            self.assertIn("market_open_live_collector_stale", reasons)
            metrics = exporter.render_prometheus(snapshot)
            self.assertIn("polymarket_v7_research_manifest_fresh 0", metrics)
            self.assertIn("polymarket_v7_live_model_scope_wired 0", metrics)

    def test_missing_graph_runtime_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            (run_root / "graph_rv" / "status.json").unlink()
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("graph_runtime_missing_or_unsafe", exporter.health_reasons(snapshot))

    def test_killed_portfolio_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "control" / "portfolio_state.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["killed"] = True
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("runtime_killed", exporter.health_reasons(snapshot))

    def test_drawdown_at_master_limit_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            path = run_root / "control" / "portfolio_state.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["drawdown"] = 0.15
            self._write(path, value)
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("drawdown_limit_breached", exporter.health_reasons(snapshot))

    def test_invalid_canonical_ledger_fails_health_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "paper_v7_live"
            self._fixture(run_root)
            (run_root / "ledger" / "execution.jsonl").write_text("not-json\n", encoding="utf-8")
            snapshot = exporter.collect_snapshot(run_root, ROOT, now=1_000)
            self.assertIn("canonical_ledger_invalid_or_mixed_sha", exporter.health_reasons(snapshot))

    def test_monitoring_manifest_and_dashboard_are_single_v7_contract(self) -> None:
        manifest = json.loads((ROOT / "monitoring" / "v7_monitoring_manifest.json").read_text(encoding="utf-8"))
        dashboard = json.loads((ROOT / manifest["grafana"]["dashboard_file"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertEqual(dashboard["uid"], "polymarket-v7")
        serialized = json.dumps(dashboard)
        for metric in (
            "polymarket_runtime_equity_usd",
            "polymarket_runtime_pnl_usd",
            "polymarket_runtime_drawdown_ratio",
            "polymarket_v7_canonical_complete_units",
            "polymarket_v7_canonical_submitted_units",
            "polymarket_v7_trade_tape_rows",
            "polymarket_v7_state_age_seconds",
            "polymarket_v7_ledger_valid",
            "polymarket_v7_research_supervisor_alive",
            "polymarket_v7_live_model_target_operational",
        ):
            self.assertIn(metric, serialized)
        for retired in ("polymarket_v7_shadow_alive", "polymarket_v7_market_proxy_markets", "polymarket_strategy_fill_rate"):
            self.assertNotIn(retired, serialized)

    def test_prometheus_alerts_cover_canonical_v7_hard_safety(self) -> None:
        config = (ROOT / "monitoring/prometheus_v7.yml").read_text(encoding="utf-8")
        alerts = (ROOT / "monitoring/v7_alerts.yml").read_text(encoding="utf-8")
        self.assertIn("__POLYMARKET_V7_ALERT_RULES__", config)
        for required in (
            "PolymarketV7ExporterDown",
            "PolymarketV7RuntimeContractInvalid",
            "PolymarketV7PaperContractInvalid",
            "PolymarketV7AuthenticatedExecutionEnabled",
            "PolymarketV7ResearchShadowSupervisorDown",
            "PolymarketV7LiveModelScopeIncomplete",
            "PolymarketV7KillSwitchEngaged",
            "PolymarketV7HardDrawdownLimitBreach",
            "PolymarketV7ExecutionOwnerDown",
            "PolymarketV7StateMissing",
            "PolymarketV7PortfolioGuardStale",
            "PolymarketV7RuntimeStateStale",
            "PolymarketV7TradeTapeEmpty",
            "PolymarketV7CanonicalLedgerInvalid",
            "polymarket_v7_authority_max_drawdown_ratio",
        ):
            self.assertIn(required, alerts)
        self.assertNotIn("PolymarketV7MarketProxyEmpty", alerts)

    def test_monitoring_sources_have_no_retired_runtime_dependency(self) -> None:
        exporter_source = EXPORTER_PATH.read_text(encoding="utf-8").lower()
        for retired in ("exporter_v6", "paper_latest", "v7_supervisor.json", "v7_execution_supervisor.json", "market_proxy_status.json", "v7_execution_evidence.json"):
            self.assertNotIn(retired, exporter_source)
        provider = (ROOT / "monitoring/grafana/provisioning/dashboards/v7.yml").read_text(encoding="utf-8")
        self.assertNotIn("/Users/enrico", provider)
        self.assertIn("__POLYMARKET_V7_DASHBOARD_DIR__", provider)
        installer = (ROOT / "ops/apply_v7_monitoring_config_macos.sh").read_text(encoding="utf-8")
        self.assertIn("v7_monitoring_manifest.json", installer)
        self.assertIn("prometheus-v7-alerts.yml", installer)
        self.assertNotIn("exporter_latest", installer)
        self.assertNotIn("paper_v6", installer)

    def test_monitoring_workflow_executes_v7_monitoring_contract(self) -> None:
        workflow = (ROOT / ".github/workflows/monitoring.yml").read_text(encoding="utf-8")
        for required in (
            "tests/test_monitoring_v7_native.py",
            "tests/test_monitoring_v7_ledger.py",
            "tests/test_monitoring_v7_dashboard_completion.py",
            "monitoring/exporter_v7.py",
            "monitoring/v7_ledger_metrics.py",
            "monitoring/v7_monitoring_manifest.json",
            "monitoring/v7_alerts.yml",
            "monitoring/grafana/dashboards/polymarket-v7.json",
            "ops/apply_v7_monitoring_config_macos.sh",
        ):
            self.assertIn(required, workflow)


if __name__ == "__main__":
    unittest.main()
