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
        sha = self._sha()
        self._write(root / "control/runtime_status.json", {
            "schema":"polymarket_v7_runtime_status_v2","timestamp":now-5,"version":7,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,"model_sha":sha,"pid":os.getpid(),
            "run_id":"run-id","economic_engines":["CRYPTO_SETTLEMENT_ENGINE","STRUCTURAL_ARB_ENGINE"],
            "economic_new_risk_ready":False,"authorized_alpha_actions":[],
        })
        budgets={"CRYPTO_SETTLEMENT_ENGINE":5000.0,"STRUCTURAL_ARB_ENGINE":4500.0}
        self._write(root / "control/allocations/manifest.json", {
            "schema":"polymarket_v7_capital_allocation_v3","paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"real_capital_at_risk":False,"account_starting_capital":10000.0,
            "capital_authority_owner_count":1,"engine_budgets":budgets,"engine_count":2,"reserve_budget":500.0,
        })
        self._write(root / "control/portfolio_state.json", {
            "schema":"polymarket_v7_portfolio_guard_v2","timestamp":now-3,"paper_only":True,
            "authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,
            "account_starting_capital":10000.0,"equity":10000.0,"peak":10000.0,"drawdown":0.0,"killed":False,
            "engines":{
                "CRYPTO_SETTLEMENT_ENGINE":{"budget":5000.0,"equity":5000.0,"killed":False},
                "STRUCTURAL_ARB_ENGINE":{"budget":4500.0,"equity":4500.0,"killed":False},
            },"sleeves":{"external":{"equity":5000.0},"hard_arb":{"equity":4500.0},"reserve":{"equity":500.0}},
        })
        self._write(root / "control/evidence_capital_allocator.json", {"schema":"polymarket_v7_evidence_capital_allocator_v2","paper_only":True,"authenticated_execution":False,"real_order_submission":False,"automatic_transfer":False})
        self._write(root / "control/fee_reward_registry.json", {"schema":"polymarket_v7_fee_reward_registry_v1","model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"unknown_fee_policy":"NON_EXECUTABLE","unknown_reward_policy":"ZERO_EXPECTED_VALUE"})
        self._write(root / "control/retention_status.json", {"schema":"polymarket_v7_retention_status_v1","timestamp":now-10,"paper_only":True,"authenticated_execution":False,"expected_sha":sha})
        self._write(root / "fast_structural/fast_arb_status.json", {
            "schema":"polymarket_v7_structural_arb_engine_status_v1","timestamp":now-5,"model_sha":sha,"state":"RUNNING",
            "paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,
            "execution_authority":"OPPORTUNITY_PROPOSAL_ONLY","capital_authority":False,"oms_authority":False,
            "inventory_authority":False,"ledger_writer_authority":False,
        })
        self._write(root / "hard_arb/status.json", {"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"equity_cost_basis":4500.0,"realized_pnl_total":0.0})
        self._write(root / "micro_maker/status.json", {"schema":"polymarket_v7_professional_maker_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"killed":False,"source":"zero_authority_budget"})
        self._write(root / "micro_maker/selector_status.json", {"schema":"polymarket_v7_maker_selector_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"state":"OPERATIONAL_BILATERAL_FLOW","ready":True,"runtime_selection_pinned":True,"candidate_rotation_pending":True,"candidate_selected_count":40})
        self._write(root / "micro_maker/rotation_status.json", {"schema":"polymarket_v7_maker_cohort_rotation_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"state":"RUNNING","rotation_count":3})
        self._write(root / "micro_maker/runtime_diagnostics.json", {"feed_connected_workers":5,"feed_messages":1234,"decisions":4321,"quote_intents":7,"reason_counts":{"NO_ECONOMIC_QUOTE":4000}})
        self._write(root / "universe/status.json", {"schema":"polymarket_v7_adaptive_universe_status_v1","timestamp_ms":(now-5)*1000,"model_sha":sha,"state":"OPERATIONAL","paper_only":True,"authenticated_execution":False,"real_order_submission":False,"discovery_exhaustive":True,"pagination_loop_guard_hit":False,"discovered_markets":2000,"eligible_markets":1800,"tier_counts":{"HOT":400,"WARM":1250,"COLD":150}})
        self._write(root / "canonical_economics.json", {"schema":"polymarket_v7_canonical_economics_v1","expected_model_sha":sha,"paper_only":True,"authenticated_execution":False,"submitted_units":0,"complete_units":0,"net_pnl":0.0,"strategy_net_pnl":{"CRYPTO_SETTLEMENT_ENGINE":0.0,"STRUCTURAL_ARB_ENGINE":0.0}})
        (root / "ledger").mkdir(parents=True,exist_ok=True); (root / "ledger/execution.jsonl").write_text("")
        (root / "trade_tape.csv").write_text(f"timestamp,received_ms,market_id,asset_id,side,price,size,trade_id\n{now-1},{(now-1)*1000},m,t,SELL,0.5,2,tr1\n")
        self._write(root / "trade_recorder_status.json", {"schema":"polymarket_v7_trade_recorder_status_v1","timestamp_ms":(now-1)*1000,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"data_plane_healthy":True,"flow_regime":"STANDARD_CLOB_TRADES_OBSERVED","conditions":1,"requests":1,"fetched":1,"errors":0,"truncated_batches":0})
        (root / "micro_maker/latency.csv").write_text("parse_ns,tx_queue_ns\n1000,6000\n")

    def test_two_engine_fixture_is_healthy_and_has_no_legacy_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); snapshot=exporter.collect_snapshot(root,ROOT,now=1000)
            self.assertEqual(exporter.health_reasons(snapshot),[])
            metrics=exporter.render_prometheus(snapshot)
            for expected in ("polymarket_v7_live_algorithm_count 2","polymarket_v7_legacy_algorithm_count 0","polymarket_v7_live_algorithm_scope_wired 1",'polymarket_v7_economic_engine_configured{engine="CRYPTO_SETTLEMENT_ENGINE"} 1','polymarket_v7_economic_engine_configured{engine="STRUCTURAL_ARB_ENGINE"} 1'):
                self.assertIn(expected,metrics)
            for removed in ("graph_rv","micro_taker","osint","sports_latency","cross_platform","wallet_intelligence"):
                self.assertNotIn(removed,metrics.lower())

    def test_runtime_cannot_add_third_algorithm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); path=root/"control/runtime_status.json"; value=json.loads(path.read_text()); value["economic_engines"].append("OLD_ENGINE"); self._write(path,value)
            self.assertIn("runtime_live_algorithms_not_exactly_two",exporter.health_reasons(exporter.collect_snapshot(root,ROOT,now=1000)))

    def test_authenticated_runtime_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); path=root/"control/runtime_status.json"; value=json.loads(path.read_text()); value["authenticated_execution"]=True; self._write(path,value)
            self.assertIn("authenticated_execution_not_disabled",exporter.health_reasons(exporter.collect_snapshot(root,ROOT,now=1000)))

    def test_empty_tape_requires_verified_no_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); (root/"trade_tape.csv").write_text("timestamp,received_ms,market_id,asset_id,side,price,size,trade_id\n")
            status=json.loads((root/"trade_recorder_status.json").read_text()); status.update({"flow_regime":"STANDARD_CLOB_NO_MATCHING_TRADES","fetched":0}); self._write(root/"trade_recorder_status.json",status)
            snapshot=exporter.collect_snapshot(root,ROOT,now=1000); self.assertEqual(exporter.health_reasons(snapshot),[]); self.assertIn("polymarket_v7_trade_tape_no_standard_clob_flow 1",exporter.render_prometheus(snapshot))

    def test_kill_and_drawdown_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"paper_v7_live"; self._fixture(root); path=root/"control/portfolio_state.json"; value=json.loads(path.read_text()); value["killed"]=True; value["drawdown"]=.15; self._write(path,value)
            reasons=exporter.health_reasons(exporter.collect_snapshot(root,ROOT,now=1000)); self.assertIn("runtime_killed",reasons); self.assertIn("drawdown_limit_breached",reasons)

    def test_dashboard_and_alerts_use_two_engine_contract(self) -> None:
        dashboard=(ROOT/"monitoring/grafana/dashboards/polymarket-v7.json").read_text().lower(); alerts=(ROOT/"monitoring/v7_alerts.yml").read_text().lower()
        self.assertIn("polymarket_v7_live_algorithm_count",dashboard)
        for removed in ("osint","research_shadow","slow_economic_shadow"):
            self.assertNotIn(removed,dashboard); self.assertNotIn(removed,alerts)


if __name__=="__main__": unittest.main()
