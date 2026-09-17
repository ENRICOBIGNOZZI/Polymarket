from __future__ import annotations

import importlib.util, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
sys.path.insert(0, str(MONITORING))
SPEC = importlib.util.spec_from_file_location("exporter_v7", MONITORING / "exporter_v7.py")
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = exporter; SPEC.loader.exec_module(exporter)


class V7NativeMonitoringTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def _sha() -> str:
        return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()

    def _fixture(self, root: Path, now: int = 1_000) -> None:
        sha = self._sha(); pid = os.getpid()
        self._write(root / "control/runtime_status.json", {
            "schema":"polymarket_v7_runtime_status_v3","timestamp":now-5,"version":7,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,
            "model_sha":sha,"pid":pid,"run_id":"run-id","economic_engines":["CRYPTO_SETTLEMENT_ENGINE"],
            "economic_new_risk_ready":False,"authorized_alpha_actions":[],
        })
        self._write(root / "control/allocations/manifest.json", {
            "schema":"polymarket_v7_capital_allocation_v3","paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"real_capital_at_risk":False,"account_starting_capital":10000.0,
            "capital_authority_owner":"V7_CANONICAL_ALLOCATOR","capital_authority_owner_count":1,
            "engine_budgets":{"CRYPTO_SETTLEMENT_ENGINE":4000.0},"engine_count":1,"reserve_budget":6000.0,
        })
        self._write(root / "control/portfolio_state.json", {
            "schema":"polymarket_v7_portfolio_guard_v2","timestamp":now-3,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,
            "account_starting_capital":10000.0,"equity":10000.0,"peak":10000.0,"drawdown":0.0,"killed":False,
            "engines":{"CRYPTO_SETTLEMENT_ENGINE":{"budget":4000.0,"equity":4000.0,"killed":False}},
        })
        self._write(root / "control/fee_reward_registry.json", {"schema":"polymarket_v7_fee_reward_registry_v1","model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"unknown_fee_policy":"NON_EXECUTABLE","unknown_reward_policy":"ZERO_EXPECTED_VALUE"})
        self._write(root / "control/retention_status.json", {"schema":"polymarket_v7_retention_status_v1","timestamp":now-10,"paper_only":True,"authenticated_execution":False,"expected_sha":sha})
        self._write(root / "micro_maker/status.json", {"schema":"polymarket_v7_professional_maker_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"killed":False,"source":"zero_authority_budget"})
        self._write(root / "micro_maker/selector_status.json", {"schema":"polymarket_v7_maker_selector_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"state":"OPERATIONAL_BILATERAL_FLOW","ready":True,"runtime_selection_pinned":True,"candidate_rotation_pending":True,"candidate_selected_count":12})
        self._write(root / "micro_maker/rotation_status.json", {"schema":"polymarket_v7_maker_cohort_rotation_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"state":"RUNNING","rotation_count":3})
        self._write(root / "micro_maker/runtime_diagnostics.json", {"feed_connected_workers":2,"feed_messages":1234,"decisions":4321,"quote_intents":7,"reason_counts":{"NO_ECONOMIC_QUOTE":4000}})
        self._write(root / "universe/status.json", {"schema":"polymarket_v7_crypto_universe_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"state":"OPERATIONAL","paper_only":True,"authenticated_execution":False,"real_order_submission":False,"discovery_exhaustive":True,"pagination_loop_guard_hit":False,"discovered_markets":12,"eligible_markets":8,"tier_counts":{"HOT":8,"WARM":0,"COLD":0}})
        self._write(root / "canonical_economics.json", {"schema":"polymarket_v7_canonical_economics_v1","expected_model_sha":sha,"paper_only":True,"authenticated_execution":False,"submitted_units":0,"complete_units":0,"net_pnl":0.0,"strategy_net_pnl":{"CRYPTO_SETTLEMENT_ENGINE":0.0}})
        (root / "ledger").mkdir(parents=True,exist_ok=True); (root / "ledger/execution.jsonl").write_text("")
        (root / "trade_tape.csv").write_text(f"timestamp,received_ms,market_id,asset_id,side,price,size,trade_id\n{now-1},{(now-1)*1000},m,t,SELL,0.5,2,tr1\n")
        self._write(root / "trade_recorder_status.json", {"schema":"polymarket_v7_trade_recorder_status_v1","timestamp_ms":(now-1)*1000,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"data_plane_healthy":True,"flow_regime":"CRYPTO_CLOB_TRADES_OBSERVED","conditions":1,"requests":1,"fetched":1,"errors":0,"truncated_batches":0})
        (root / "micro_maker/latency.csv").write_text("parse_ns,decision_ns,tx_queue_ns\n1000,3000,6000\n2000,4000,8000\n")

    def test_crypto_only_fixture_is_healthy_and_has_no_structural_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); snapshot=exporter.collect_snapshot(root,ROOT,now=1000)
            self.assertEqual(exporter.health_reasons(snapshot),[])
            metrics=exporter.render_prometheus(snapshot)
            self.assertIn("polymarket_v7_live_algorithm_count 1",metrics)
            self.assertIn("polymarket_v7_live_algorithm_scope_wired 1",metrics)
            self.assertIn('polymarket_v7_economic_engine_configured{engine="CRYPTO_SETTLEMENT_ENGINE"} 1',metrics)

    def test_professional_maker_latency_is_exported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root)
            snapshot=exporter.collect_snapshot(root,ROOT,now=1000); latency=snapshot["maker_latency"]
            self.assertTrue(latency["present"]); self.assertTrue(latency["sources"]["professional_maker"])
            self.assertIn("parse_ns",latency["stages"]); self.assertIn("decision_ns",latency["stages"])
            metrics=exporter.render_prometheus(snapshot)
            self.assertIn('stage="decision_ns"',metrics)
            self.assertIn('polymarket_v7_latency_source_present{source="professional_maker"} 1',metrics)
        dashboard=(ROOT/"monitoring/grafana/dashboards/polymarket-v7-latency.json").read_text().lower()

    def test_metadata_plane_is_observable_but_not_part_of_execution_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root)
            universe=json.loads((root/"universe/status.json").read_text())
            universe.update({"source":"DEGRADED_CACHE","clob_complete":False,"clob_error_present":True,
                             "gamma_fallback_requests":4,"gamma_fallback_errors":4,
                             "cache_fallback_markets":2,"missing_markets":1})
            self._write(root/"universe/status.json",universe)
            metrics=exporter.render_prometheus(exporter.collect_snapshot(root,ROOT,now=1000))
            self.assertIn('polymarket_v7_universe_source_mode_info{mode="DEGRADED_CACHE"} 1',metrics)
            self.assertIn("polymarket_v7_universe_gamma_fallback_errors 4",metrics)
            self.assertIn("polymarket_v7_universe_cache_fallback_markets 2",metrics)
        launcher=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8").lower()
        self.assertNotIn("grafana server",launcher)
        self.assertNotIn("prometheus --",launcher)

    def test_runtime_cannot_add_second_algorithm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); path=root/"control/runtime_status.json"; value=json.loads(path.read_text()); value["economic_engines"].append("OLD_ENGINE"); self._write(path,value)
            self.assertIn("runtime_live_algorithms_not_crypto_only",exporter.health_reasons(exporter.collect_snapshot(root,ROOT,now=1000)))

    def test_authenticated_runtime_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); path=root/"control/runtime_status.json"; value=json.loads(path.read_text()); value["authenticated_execution"]=True; self._write(path,value)
            self.assertIn("authenticated_execution_not_disabled",exporter.health_reasons(exporter.collect_snapshot(root,ROOT,now=1000)))

    def test_empty_tape_requires_verified_no_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); (root/"trade_tape.csv").write_text("timestamp,received_ms,market_id,asset_id,side,price,size,trade_id\n")
            status=json.loads((root/"trade_recorder_status.json").read_text()); status.update({"flow_regime":"CRYPTO_CLOB_NO_MATCHING_TRADES","fetched":0}); self._write(root/"trade_recorder_status.json",status)
            snapshot=exporter.collect_snapshot(root,ROOT,now=1000); self.assertEqual(exporter.health_reasons(snapshot),[]); self.assertIn("polymarket_v7_trade_tape_no_standard_clob_flow 1",exporter.render_prometheus(snapshot))

    def test_kill_and_drawdown_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); path=root/"control/portfolio_state.json"; value=json.loads(path.read_text()); value["killed"]=True; value["drawdown"]=.15; self._write(path,value)
            reasons=exporter.health_reasons(exporter.collect_snapshot(root,ROOT,now=1000)); self.assertIn("runtime_killed",reasons); self.assertIn("drawdown_limit_breached",reasons)

    def test_dashboard_and_alerts_are_crypto_only(self) -> None:
        dashboard=(ROOT/"monitoring/grafana/dashboards/polymarket-v7.json").read_text().lower(); alerts=(ROOT/"monitoring/v7_alerts.yml").read_text().lower()
        self.assertIn("polymarket_v7_live_algorithm_count",dashboard)
        self.assertNotIn("structural",dashboard); self.assertNotIn("structural",alerts)


if __name__=="__main__": unittest.main()
