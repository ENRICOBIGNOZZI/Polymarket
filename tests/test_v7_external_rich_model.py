from __future__ import annotations
import copy, json, math, sys, tempfile, unittest, importlib.util
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from v7_external_rich_model import (FEATURE_SCHEMA, FAMILY, HISTORY_SEMANTICS, features,
    contextual_features, predict, validate_parameters, is_paper_learning_fair)
from v7_external_rich_train import build_rows, split_rows, train
from v7_fair_model_artifact import FairModelArtifact, canonical_hash


def origin(i=0):
    start=1_700_000_100+i*300
    return dict(observed_ms=start*1000+100_000, oracle_value=100+(.1 if i%2 else -.1),
        reference_value=100, observed_tte_seconds=200, external_features=dict(
        composite_price=100+(.15 if i%2 else -.15), composite_microprice=100+(.14 if i%2 else -.14),
        dispersion_bps=1+i%4,age_ns=20_000_000,aggregate_ofi=(i%7-3)*.01,
        aggregate_trade_imbalance=(i%5-2)*.03,realized_vol_fast=.001+i%3*.0001,
        realized_vol_medium=.0012,realized_vol_slow=.0014,return_1s=0.,return_5s=0.))


def rows():
    out=[]
    for i in range(100):
        o=origin(i); start=1_700_000_100+i*300
        out.append(dict(market_id=str(i),forecast_id='f'+str(i),market_start_ms=start*1000,
            observed_ms=o['observed_ms'],label_received_ms=(start+301)*1000,
            actual=float(i%2),market_probability=.55 if i%2 else .45,features=features(o),
            rules_hash='b'*64,origin_record_id='o'+str(i),final_record_id='e'+str(i),source_code_sha='a'*40))
    return out


class RichModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows=rows();cls.artifact,cls.report=train(cls.rows,'a'*40,'c'*64,[],1_800_000_000_000_000_000)
    def test_verified_schema_and_prediction(self):
        a=self.artifact;validate_parameters(a)
        self.assertEqual(a.family,FAMILY);self.assertFalse(a.probability_interval_diagnostics['validated'])
        self.assertTrue(0 < predict(a,self.rows[-1]['features'],.55) < 1)
        self.assertTrue(a.hyperparameters['research_only'])
    def test_old_missing_history_never_flat_feature(self):
        o=origin(); self.assertIsNone(features(o)['return_5s_bp'])
        o['external_features'].update(feature_semantics_version=HISTORY_SEMANTICS,return_history_available={'5s':True})
        self.assertEqual(features(o)['return_5s_bp'],0.)
        o['external_features']['return_history_available']['5s']=False
        self.assertIsNone(features(o)['return_5s_bp'])
    def test_future_context_rejected(self):
        o=origin();o['external_context']={'observed_wall_ns':o['observed_ms']*1_000_000+2_000_000,'features':{}}
        with self.assertRaises(ValueError):features(o)
    def test_optional_future_slow_source_excluded(self):
        out=contextual_features({}, {'state':'OPERATIONAL','latest':{'open_interest_velocity':7,
            'open_interest_request':{'local_receive_wall_ns':200}}}, {}, 100)
        self.assertNotIn('binance_oi_velocity',out['features']);self.assertTrue(out['exclusions'])
    def test_label_embargo_and_disjoint_markets(self):
        p=split_rows(self.rows)
        sets=[{r['market_id'] for r in v} for v in p.values()]
        self.assertFalse(sets[0]&sets[1] or sets[1]&sets[2] or sets[0]&sets[2])
        self.assertLess(max(r['label_received_ms'] for r in p['train']), min(r['market_start_ms'] for r in p['validation']))
    def test_sparse_real_derivative_feature_is_not_dropped_by_old_90pct_gate(self):
        changed=copy.deepcopy(self.rows)
        for i,row in enumerate(changed[:12]):
            row['features']['binance_perp_basis_bp']=float(i)-5.5
        artifact,_=train(changed,'a'*40,'c'*64,[],1_800_000_000_000_000_000)
        self.assertIn('binance_perp_basis_bp',artifact.parameters['feature_names'])
        coverage=artifact.parameters['feature_coverage']['binance_perp_basis_bp']['market_weight_fraction']
        self.assertLess(coverage,.90)
        self.assertGreater(coverage,.05)

    def test_audit_labels_do_not_select_model(self):
        changed=copy.deepcopy(self.rows)
        for r in changed[80:]:r['actual']=1-r['actual']
        a,_=train(changed,'a'*40,'c'*64,[],1_800_000_000_000_000_000)
        self.assertEqual(a.parameters,self.artifact.parameters)
    def test_shrinkage_grid_includes_market_baseline(self):
        self.assertTrue(any(c['ridge']==10000. and c['correction_shrinkage']==0.
                            for c in self.report['candidate_validation']))
        self.assertLessEqual(self.report['split_scores']['validation']['brier'],
                             self.report['market_baseline_scores']['validation']['brier']+1e-12)
    def test_future_training_label_rejected(self):
        with self.assertRaisesRegex(ValueError,'future_training_label'):
            train(self.rows,'a'*40,'c'*64,[],1_600_000_000_000_000_000)
    def test_hash_tampering_rejected(self):
        a=copy.deepcopy(self.artifact);a.parameters['coefficients'][0]+=1
        with self.assertRaises(ValueError):validate_parameters(a)
    def test_research_probe_has_no_real_authority(self):
        f=dict(valid=True,paper_exploration_learned=True,research_model=True,
            research_model_state='FROZEN_INFERENCE_ONLY',real_money_authority=False,
            probability_interval_validated=False,lower=0.,upper=1.,family=FAMILY,
            inference_state='VALID_PAPER_LEARNED_PROBE',authority='SHADOW',
            probability_model_hash=self.artifact.model_hash,probability_model_id=self.artifact.model_version)
        self.assertTrue(is_paper_learning_fair(f))
        for k,v in [('real_money_authority',True),('lower',.01),('research_model',False),
                    ('research_model_state','MUTABLE')]:
            self.assertFalse(is_paper_learning_fair({**f,k:v}))
    def learned_fair(self):
        return dict(valid=True,paper_exploration_learned=True,research_model=True,
            research_model_state='FROZEN_INFERENCE_ONLY',paper_exploration_bootstrap=False,
            real_money_authority=False,probability_interval_validated=False,lower=0.,upper=1.,yes=.70,
            family=FAMILY,inference_state='VALID_PAPER_LEARNED_PROBE',authority='SHADOW',
            probability_model_hash=self.artifact.model_hash,probability_model_id=self.artifact.model_version)
    def load_fixture(self, name):
        path=Path(__file__).with_name(name+'.py')
        spec=importlib.util.spec_from_file_location(name,path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
    def test_learned_maker_uses_same_typed_capped_probe(self):
        m=self.load_fixture('test_v7_maker_opportunity_bridge')
        with tempfile.TemporaryDirectory() as directory:
            r=Path(directory);m.setup_run(r,mature=True)
            status=m.fair_status();status['fair'].update(self.learned_fair())
            m.write(r/'external_fair/status.json',status)
            with mock.patch.object(m.bridge,'_paper_crypto_context',return_value=m.context()):
                opportunities,diag=m.bridge.build_maker_opportunities(r,now_ns=2_000_000_000,repository_root=m.ROOT)
            self.assertEqual(len(opportunities),1,diag)
            parsed=m.OpportunityEnvelope.parse(opportunities[0]).raw
            self.assertTrue(parsed['exploration']['research_only'])
            self.assertFalse(parsed['exploration']['robust_candidate'])
            self.assertLessEqual(parsed['exploration']['maximum_probe_loss'],2.)
    def test_learned_taker_respects_explicit_switch_and_hard_guards(self):
        m=self.load_fixture('test_v7_external_fair_paper_router')
        status=m.snapshot();status['fair'].update(self.learned_fair())
        policy=json.loads((Path(__file__).resolve().parents[1]/'config/v7_external_fair.json').read_text())
        probes=m.router.validate_probe_policy(policy['paper_exploration_probe'])
        books={'yes':m.book('yes',.52,.50),'no':m.book('no',.50,.48)}
        self.assertTrue(m.router.paper_probe_candidates(status,books,policy['taker'],probes))
        disabled=dict(probes);disabled['allow_frozen_rich_ml']=False
        self.assertEqual(m.router.paper_probe_candidates(status,books,policy['taker'],disabled),[])
        status['real_order_submission']=True
        self.assertEqual(m.router.paper_probe_candidates(status,books,policy['taker'],probes),[])
    def test_rich_taker_revalidates_pm_prior_at_arrival(self):
        m=self.load_fixture('test_v7_external_fair_paper_router')
        status=m.snapshot();fair=self.learned_fair();fair.update(
            uses_polymarket_price_as_feature=True,market_prior_causal_cut_valid=True,
            pm_mid=.50,pm_mid_receive_ts_ms=1001)
        status['fair'].update(fair)
        policy=json.loads((Path(__file__).resolve().parents[1]/'config/v7_external_fair.json').read_text())
        probes=m.router.validate_probe_policy(policy['paper_exploration_probe'])
        books={'yes':m.book('yes',.52,.50),'no':m.book('no',.50,.48)}
        self.assertTrue(m.router.paper_probe_candidates(status,books,policy['taker'],probes))
        status['fair']['pm_mid']=.40
        self.assertEqual(m.router.paper_probe_candidates(status,books,policy['taker'],probes),[])
        status['fair']['pm_mid']=.50;status['fair']['pm_mid_receive_ts_ms']=1
        self.assertEqual(m.router.paper_probe_candidates(status,books,policy['taker'],probes),[])

    def test_runtime_is_inference_only_unless_explicit_freeze_is_requested(self):
        config=json.loads((Path(__file__).resolve().parents[1]/'config/v7_external_fair.json').read_text())
        ml=config['paper_ml_probe']
        self.assertFalse(ml['runtime_training']);self.assertTrue(ml['inference_only_runtime'])
        self.assertEqual(ml['training_lifecycle'],'EXPLICIT_FROZEN_ARTIFACT_ONLY')
        launcher=(Path(__file__).resolve().parents[1]/'scripts/paper_v7_execution_loop.sh').read_text()
        guard='if [[ "${PM_V7_FREEZE_RICH_MODEL:-0}" == "1" ]]; then'
        self.assertIn(guard,launcher)
        self.assertIn('{"state":"INFERENCE_ONLY","runtime_training":false}',launcher)

    def test_liquidation_rates_are_receive_time_causal_and_reset_safe(self):
        import v7_rtds_external_fair_monitor as module
        with tempfile.TemporaryDirectory() as directory:
            monitor=module.Monitor(Path(directory),'a'*40)
            first={"timestamp_ns":1_000_000_000,"composite_price":100.0,
                   "binance_usdm":{"valid":True,"liquidation_buy_notional":100.0,
                                   "liquidation_sell_notional":50.0},
                   "bybit_linear":{"valid":True,"liquidation_buy_notional":0.0,
                                   "liquidation_sell_notional":0.0}}
            self.assertEqual(monitor.liquidation_rate_features(first,1_000_000_000),{})
            second={"timestamp_ns":2_000_000_000,"composite_price":100.0,
                    "binance_usdm":{"valid":True,"liquidation_buy_notional":120.0,
                                    "liquidation_sell_notional":60.0},
                    "bybit_linear":{"valid":True,"liquidation_buy_notional":5.0,
                                    "liquidation_sell_notional":15.0}}
            rates=monitor.liquidation_rate_features(second,2_000_000_000)
            self.assertAlmostEqual(rates["binance_liquidation_signed_btc_per_s"],.1)
            self.assertAlmostEqual(rates["binance_liquidation_total_btc_per_s"],.3)
            self.assertAlmostEqual(rates["bybit_liquidation_signed_btc_per_s"],-.1)
            reset={"timestamp_ns":3_000_000_000,"composite_price":100.0,
                   "binance_usdm":{"valid":True,"liquidation_buy_notional":1.0,
                                   "liquidation_sell_notional":1.0}}
            self.assertNotIn("binance_liquidation_total_btc_per_s",
                             monitor.liquidation_rate_features(reset,3_000_000_000))

    def test_external_snapshot_rejects_future_publication(self):
        import v7_rtds_external_fair_monitor as module
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'external.json'
            p.write_text(json.dumps({'code_sha':'a'*40,'timestamp_ns':101}))
            monitor=module.Monitor(Path(directory),'a'*40,external_venues_path=p)
            self.assertEqual(monitor.external_snapshot(100),{})
            self.assertEqual(monitor.external_snapshot(102)['age_ns'],1)
    def test_live_rich_inference_original_feature_cut_and_forward_scope(self):
        import v7_rtds_external_fair_monitor as module
        with tempfile.TemporaryDirectory() as directory:
            monitor=module.Monitor(Path(directory),'a'*40)
            monitor.research_model=self.artifact;monitor.research_model_load_state='LOADED'
            boundary=self.artifact.hyperparameters['forward_oos_starts_after_ns'];now=boundary+100_000_000_000
            monitor.active_market={'contract_start_epoch':boundary//1_000_000_000}
            monitor.active_contract={'normalized_rules_hash':'b'*64}
            monitor.reference={'valid':True,'value':100.}
            monitor.latest[module.ORACLE_TOPIC]={'price':100.1}
            base={'valid':True,'tte_seconds':200.,'pm_mid':.5}
            ext=origin()['external_features'];context={'observed_wall_ns':now,'features':{}}
            fair=monitor.rich_paper_snapshot(base,ext,context,now)
            self.assertTrue(is_paper_learning_fair(fair,'a'*40),fair)
            self.assertEqual(canonical_hash(fair['rich_feature_cut']),fair['rich_feature_sha256'])
            self.assertEqual(features(fair['rich_feature_cut']),fair['rich_model_features'])
            monitor.active_market['contract_start_epoch']-=300
            self.assertFalse(monitor.rich_paper_snapshot(base,ext,context,now)['valid'])

    def test_market_offset_rich_model_requires_identified_fresh_causal_pm_prior_in_production(self):
        import v7_rtds_external_fair_monitor as module
        with tempfile.TemporaryDirectory() as directory:
            monitor=module.Monitor(Path(directory),'a'*40)
            monitor.research_model=self.artifact;monitor.research_model_load_state='LOADED'
            monitor.paper_ml_policy={'causal_pm_prior_required':True,
                'maximum_pm_prior_age_ms':500.0,'maximum_pm_external_skew_ms':750.0}
            boundary=self.artifact.hyperparameters['forward_oos_starts_after_ns'];now=boundary+100_000_000_000
            now_ms=now//1_000_000
            monitor.active_market={'contract_start_epoch':boundary//1_000_000_000}
            monitor.active_contract={'normalized_rules_hash':'b'*64}
            monitor.reference={'valid':True,'value':100.}
            monitor.latest[module.ORACLE_TOPIC]={'price':100.1}
            base={'valid':True,'tte_seconds':200.,'pm_mid':.5,
                'pm_mid_snapshot_id':'pm-snap-1','pm_mid_receive_ts_ms':now_ms-100,
                'pm_mid_exchange_ts_ms':now_ms-101,'pm_mid_age_ms':100}
            ext=origin()['external_features'];ext['timestamp_ns']=now-50_000_000
            context={'observed_wall_ns':now,'features':{}}
            fair=monitor.rich_paper_snapshot(base,ext,context,now)
            self.assertTrue(fair['valid'],fair)
            self.assertTrue(fair['market_prior_causal_cut_valid'])
            self.assertEqual(fair['market_prior_snapshot_id'],'pm-snap-1')
            stale=dict(base);stale['pm_mid_age_ms']=501
            self.assertEqual(monitor.rich_paper_snapshot(stale,ext,context,now)['reason'],
                             'PM_PRIOR_STALE_OR_UNIDENTIFIED')
            skewed=dict(ext);skewed['timestamp_ns']=now+700_000_000
            self.assertEqual(monitor.rich_paper_snapshot(base,skewed,context,now)['reason'],
                             'PM_EXTERNAL_CAUSAL_SKEW')

    def test_direct_research_model_loader_has_no_sha_deployment_coupling(self):
        import v7_rtds_external_fair_monitor as monitor
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'research.json';path.write_text(json.dumps(self.artifact.__dict__))
            loaded,state=monitor.load_rich_research_model(path)
            self.assertEqual(state,'LOADED');self.assertEqual(loaded.model_hash,self.artifact.model_hash)
            # Runtime code SHA is intentionally not part of this loader contract.
            self.assertNotEqual(loaded.code_sha,'d'*40)
            raw=json.loads(path.read_text());raw['artifact_role']='INVALID_ROLE';path.write_text(json.dumps(raw))
            loaded,state=monitor.load_rich_research_model(path)
            self.assertIsNone(loaded);self.assertEqual(state,'RESEARCH_MODEL_INVALID')

    def test_builder_requires_verified_binary_settlement(self):
        o=origin();common=dict(schema='polymarket_v7_external_fair_counterfactual_v1',
            evidence_semantics_version='external-fair-settlement-evidence-v2',paper_only=True,
            authenticated_execution=False,real_order_submission=False,execution_authority='SHADOW_ZERO_AUTHORITY',
            forecast_id='x',market_id='m',model_sha='a'*40,policy_sha256='c'*64)
        o.update(common,event_type='FORECAST',reference_version=1_700_000_100,market_yes=.5,rules_hash='b'*64,
            yes_token='Y',no_token='N',market_mid_source='LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH',record_id='o')
        f=dict(common,event_type='FORECAST_FINAL',timestamp_ms=1_700_000_401_000,
            settlement_observed_ms=1_700_000_401_000,actual_yes=1.,settlement_closed=True,
            settlement_provider='POLYMARKET_GAMMA_PUBLIC',settlement_token_ids=['Y','N'],
            settlement_outcome_prices=[1.,0.],winning_token_id='Y',record_id='f')
        out,_=build_rows([o,f],1_800_000_000_000);self.assertEqual(len(out),1)
        f['settlement_closed']=False
        out,_=build_rows([o,f],1_800_000_000_000);self.assertFalse(out)

if __name__=='__main__':unittest.main()
