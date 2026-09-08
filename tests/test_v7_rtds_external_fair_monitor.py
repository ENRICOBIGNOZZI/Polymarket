#!/usr/bin/env python3
from __future__ import annotations
import json, sys, tempfile, time, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
import v7_rtds_external_fair_monitor as module
from v7_external_rich_model import FAMILY, FEATURE_SCHEMA
from v7_fair_model_artifact import FairModelArtifact


def research_artifact() -> FairModelArtifact:
    return FairModelArtifact.build(family=FAMILY,model_version="btc-m5-rich-logit-test",
        feature_schema_version=FEATURE_SCHEMA,code_sha="a"*40,policy_version="p",artifact_role="RESEARCH",
        training_start_ns=1,training_end_ns=2,training_contracts=100,training_days=1,assets=("BTC",),
        contract_templates=("BTC_USD_UPDOWN_5M",),rules_hashes=("b"*64,),
        parameters={"feature_names":["log_tte"],"means":[5.0],"scales":[1.0],
            "offset":"market","missing_policy":"TRAIN_MEAN_AND_EXPLICIT_INDICATOR",
            "coefficients":[0.0,0.1,0.0],"ridge":1.0,"excluded_features":{}},
        hyperparameters={"research_only":True,"forward_oos_starts_after_ns":0},oos_scores={},
        probability_interval_diagnostics={"validated":False,"bounds":[0.0,1.0]},economic_replay={},
        generated_timestamp_ns=3)


class MonitorTests(unittest.TestCase):
    def test_observations_decode_oracle_and_external(self):
        rows=list(module.observations([
            {"topic":"crypto_prices_twap_sixty","payload":[{"symbol":"btc/usd","timestamp":1000,"value":77000,"window_s":60}]},
            {"topic":"crypto_prices","payload":{"symbol":"BTCUSDT","timestamp":1001,"value":"77001"}},]))
        self.assertEqual([r["topic"] for r in rows],[module.ORACLE_TOPIC,module.EXTERNAL_TOPIC])
        self.assertEqual(rows[0]["window_seconds"],60);self.assertEqual(rows[1]["price"],77001.0)

    def test_boundary_reference_is_causal_and_bounded(self):
        h={7000:{"timestamp_ms":7000,"price":70},9000:{"timestamp_ms":9000,"price":90},11000:{"timestamp_ms":11000,"price":110}}
        self.assertEqual(module.boundary_reference(h,10000)["timestamp_ms"],9000)
        self.assertIsNone(module.boundary_reference({7000:h[7000]},10000))
        self.assertIsNone(module.boundary_reference({11000:h[11000]},10000))

    def test_router_snapshot_preserves_causal_identity_and_age(self):
        router={"code_sha":"a"*40,"live_market":{"valid":True,"source":"LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
            "market_id":"m","yes":.55,"snapshot_id":"s","receive_ts_ms":1000,"exchange_ts_ms":999}}
        snap=module.router_live_market_snapshot(router,code_sha="a"*40,market_id="m",now_ms=1100)
        self.assertEqual(snap["snapshot_id"],"s");self.assertEqual(snap["age_ms"],100)
        self.assertIsNone(module.router_live_market_snapshot(router,code_sha="a"*40,market_id="m",now_ms=7000))

    def test_fast_pm_prior_is_preferred_and_stale_fast_falls_back(self):
        fast={"code_sha":"a"*40,"live_market":{"valid":True,"source":"LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
            "market_id":"m","yes":.61,"snapshot_id":"fast","receive_ts_ms":1080,"exchange_ts_ms":1079}}
        slow={"code_sha":"a"*40,"live_market":{"valid":True,"source":"LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
            "market_id":"m","yes":.55,"snapshot_id":"slow","receive_ts_ms":1000,"exchange_ts_ms":999}}
        snap=module.preferred_pm_prior_snapshot(fast,slow,code_sha="a"*40,market_id="m",now_ms=1100)
        self.assertEqual(snap["snapshot_id"],"fast")
        fast["live_market"]["receive_ts_ms"]=0
        snap=module.preferred_pm_prior_snapshot(fast,slow,code_sha="a"*40,market_id="m",now_ms=1100)
        self.assertEqual(snap["snapshot_id"],"slow")

    def test_external_snapshot_rejects_future_or_wrong_sha(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"external.json";p.write_text(json.dumps({"code_sha":"a"*40,"timestamp_ns":101,"valid":True}))
            m=module.Monitor(Path(d),"a"*40,external_venues_path=p)
            self.assertEqual(m.external_snapshot(100),{})
            self.assertEqual(m.external_snapshot(102)["age_ns"],1)

    def test_structural_bootstrap_is_active_fallback_without_model_governance(self):
        cfg=json.loads((ROOT/"config/v7_external_fair.json").read_text())
        m=module.Monitor(Path(tempfile.mkdtemp()),"a"*40,paper_bootstrap=cfg["paper_exploration_bootstrap"])
        start=1_800_000_000;now=(start+120)*1_000_000_000
        m.active_market={"contract_start_epoch":start,"midpoint":.5};m.active_contract={"verified_template":True,
            "rules_hash_recognized":True,"normalized_rules_hash":"b"*64};m.reference={"valid":True,"value":77000.0}
        m.latest[module.ORACLE_TOPIC]={"price":77010.0,"receive_wall_ns":now-10_000_000}
        snap=m.fair_snapshot(now,True,{"valid":True,"fresh_venue_count":3,"composite_price":77020.0,
            "dispersion_bps":1.0,"age_ns":10_000_000,"return_1s":0.0,"return_5s":0.0})
        self.assertTrue(snap["valid"]);self.assertTrue(snap["paper_exploration_bootstrap"])
        self.assertEqual(snap["calibration_state"],"PAPER_EXPLORATION_BOOTSTRAP_APPLIED")
        self.assertFalse(snap["real_money_authority"])

    def test_direct_research_model_loads_without_runtime_sha_coupling(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"research.json";a=research_artifact();path.write_text(json.dumps(a.__dict__))
            loaded,state=module.load_rich_research_model(path)
            self.assertEqual(state,"LOADED");self.assertEqual(loaded.model_hash,a.model_hash)

    def test_rich_market_prior_requires_fresh_identified_causal_cut(self):
        a=research_artifact();m=module.Monitor(Path(tempfile.mkdtemp()),"d"*40)
        m.research_model=a;m.research_model_load_state="LOADED";m.paper_ml_policy={"causal_pm_prior_required":True,
            "maximum_pm_prior_age_ms":500.0,"maximum_pm_external_skew_ms":750.0}
        now=1_800_000_000_000_000_000;m.active_market={"contract_start_epoch":int(now/1e9)//300*300}
        m.active_contract={"normalized_rules_hash":"b"*64};m.reference={"valid":True,"value":100.0};m.latest[module.ORACLE_TOPIC]={"price":100.1}
        base={"valid":True,"tte_seconds":200.,"pm_mid":.5,"pm_mid_snapshot_id":"s",
            "pm_mid_receive_ts_ms":now//1_000_000-100,"pm_mid_exchange_ts_ms":now//1_000_000-101,"pm_mid_age_ms":100}
        ext={"timestamp_ns":now-50_000_000,"composite_price":100.2,"composite_microprice":100.2,"dispersion_bps":1.,
            "age_ns":1_000_000,"aggregate_ofi":0.,"aggregate_trade_imbalance":0.,"realized_vol_fast":.001,
            "realized_vol_medium":.001,"realized_vol_slow":.001,"feature_semantics_version":"receive_time_bucketed_composite_v2",
            "return_history_available":{"100ms":True,"250ms":True,"1s":True,"5s":True},"return_100ms":0.,"return_250ms":0.,"return_1s":0.,"return_5s":0.}
        fair=m.rich_paper_snapshot(base,ext,{"observed_wall_ns":now,"features":{}},now)
        self.assertTrue(fair["valid"],fair);self.assertTrue(fair["research_model"]);self.assertTrue(fair["market_prior_causal_cut_valid"])
        stale=dict(base);stale["pm_mid_age_ms"]=501
        self.assertEqual(m.rich_paper_snapshot(stale,ext,{"observed_wall_ns":now,"features":{}},now)["reason"],"PM_PRIOR_STALE_OR_UNIDENTIFIED")

    def test_launcher_has_single_frozen_research_model_path(self):
        text=(ROOT/"scripts/paper_v7_execution_loop.sh").read_text()
        self.assertIn('--research-model "$RICH_RESEARCH_MODEL"',text)

if __name__=="__main__":unittest.main()
