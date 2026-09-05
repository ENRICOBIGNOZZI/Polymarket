import copy
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_external_fair_challenger as model
from v7_fair_value_registry import FairModelArtifact
SHA = "a" * 40
RULES = "b" * 64


def fixture(policy_hash, count=24):
    rows=[]
    for i in range(count):
        p=0.25+0.02*i; y=float(i%2); observed=1_000_000+i*400_000
        common={"schema":"polymarket_v7_external_fair_counterfactual_v1",
            "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
            "execution_authority":"SHADOW_ZERO_AUTHORITY","model_sha":SHA,
            "model_version":model.MODEL_VERSION,"policy_sha256":policy_hash,
            "evidence_semantics_version":model.EVIDENCE_SEMANTICS_VERSION,
            "forecast_id":f"f-{i}","market_id":f"m-{i}"}
        rows.append({**common,"record_id":f"o-{i}","event_type":"FORECAST",
            "rules_hash":RULES,"external_only_yes":p,"model_yes":p,"market_yes":0.5,
            "timestamp_ms":observed,"observed_tte_seconds":120.0,"oracle_value":100.0,
            "reference_value":100.0,"yes_token":f"y-{i}","no_token":f"n-{i}",
            "market_mid_source":"LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",
            "external_features":{"composite_price":100+0.01*(i-12),"return_1s":(i-12)*0.00001,"return_5s":0.0}})
        rows.append({**common,"record_id":f"t-{i}","event_type":"FORECAST_FINAL",
            "timestamp_ms":observed+350_000,"settlement_observed_ms":observed+350_000,
            "actual_yes":y,"settlement_closed":True,"settlement_provider":"POLYMARKET_GAMMA_PUBLIC",
            "settlement_token_ids":[f"y-{i}",f"n-{i}"],"settlement_outcome_prices":[y,1-y],
            "winning_token_id":f"y-{i}" if y else f"n-{i}"})
    return rows


class ExternalFairChallengerTests(unittest.TestCase):
    def test_freezes_once_with_verified_v2_labels_and_future_contract_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp); config=root/'config.json'
            config.write_text(json.dumps({"paper_only":True,"execution_authority":"SHADOW_ZERO_AUTHORITY"}))
            policy=model.canonical_policy_hash(config); tape=root/'tape.jsonl'
            values=fixture(policy); tape.write_text(''.join(json.dumps(r)+'\n' for r in values))
            kwargs=dict(tape_paths=[tape],registry_root=root/'registry',config_path=config,model_sha=SHA,status_path=root/'status.json')
            first=model.freeze_challenger(**kwargs)
            self.assertEqual(first['state'],'FROZEN_CHALLENGER_PUBLISHED')
            pointer=json.loads((root/'registry/fair_value_challenger.json').read_text())
            artifact=FairModelArtifact(**json.loads(pathlib.Path(pointer['artifact']).read_text()))
            artifact.validate(); model.validate_residual_parameters(artifact)
            self.assertEqual(artifact.training_contracts,24)
            self.assertLess(artifact.training_end_ns,artifact.generated_timestamp_ns)
            self.assertGreater(artifact.hyperparameters['forward_oos_starts_after_ns'],artifact.generated_timestamp_ns)
            self.assertFalse(artifact.hyperparameters['uses_polymarket_price_as_feature'])
            self.assertFalse(artifact.hyperparameters['protocol']['automatic_promotion'])
            self.assertFalse((root/'registry/fair_value_champion.json').exists())
            dataset=pathlib.Path(artifact.hyperparameters['training_dataset']).read_bytes()
            self.assertEqual(hashlib.sha256(dataset).hexdigest(),artifact.hyperparameters['training_dataset_sha256'])
            # Appending new evidence must not refit or move the frozen boundary.
            with tape.open('a') as h:h.write(json.dumps({'record_id':'new','event_type':'OTHER'})+'\n')
            second=model.freeze_challenger(**kwargs)
            self.assertEqual(second['model_hash'],first['model_hash'])
            self.assertEqual(second['state'],'FROZEN_CHALLENGER_REUSED')
            rows=model.settled_rows(values,policy_sha256=policy)
            a=model.predict_residual(artifact,rows[0]['probability'],rows[0]['features'])
            self.assertTrue(0<a<1)
            features=copy.deepcopy(rows[0]['features']); features['market_yes']=0.99
            self.assertEqual(a,model.predict_residual(artifact,rows[0]['probability'],features))
            features.pop(model.RESIDUAL_FEATURES[0])
            with self.assertRaisesRegex(ValueError,'missing_residual_feature'):
                model.predict_residual(artifact,rows[0]['probability'],features)

    def test_cutoff_and_semantics_do_not_leak_future_labels(self):
        rows=fixture('p',4)
        selected=model.settled_rows(rows,policy_sha256='p',cutoff_ms=1_500_000)
        self.assertEqual([r['market_id'] for r in selected],['m-0'])
        old=copy.deepcopy(rows)
        for r in old:r['evidence_semantics_version']='external-fair-settlement-evidence-v1'
        self.assertEqual(model.settled_rows(old,policy_sha256='p'),[])
        uncertain=copy.deepcopy(rows);uncertain[1]['settlement_closed']=False
        self.assertEqual(len(model.settled_rows(uncertain,policy_sha256='p')),3)
        bad=copy.deepcopy(rows);bad[1]['actual_yes']=1-bad[1]['actual_yes']
        with self.assertRaisesRegex(ValueError,'settlement_label_mismatch'):
            model.settled_rows(bad,policy_sha256='p')

    def test_conflicts_and_corruption_fail_closed_even_unselected(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=pathlib.Path(tmp)/'a';b=pathlib.Path(tmp)/'b'
            a.write_text('{"record_id":"x","event_type":"OTHER","v":1}\n')
            b.write_text('{"record_id":"x","event_type":"OTHER","v":2}\n')
            with self.assertRaisesRegex(ValueError,'evidence_record_conflict'):model.records([a,b])
            b.write_text('not json\n')
            with self.assertRaises(ValueError):model.records([b])
            b.write_text('{"record_id":"incomplete"')
            self.assertEqual(model.records([b]),{})

    def test_training_is_deterministic_and_cluster_weighted(self):
        rows=model.settled_rows(fixture('p'),policy_sha256='p')
        first=model.fit_residual(rows)
        self.assertEqual(first,model.fit_residual(rows))
        doubled=[r for row in rows for r in (row,copy.deepcopy(row))]
        second=model.fit_residual(doubled)
        for key in ['calibration_intercept','calibration_slope']:
            self.assertAlmostEqual(first[key],second[key],places=7)
        for a,b in zip(first['feature_coefficients'],second['feature_coefficients']):
            self.assertAlmostEqual(a,b,places=7)



class ForwardEconomicTests(unittest.TestCase):
    def artifact(self):
        return FairModelArtifact.build(family=model.RESIDUAL_FAMILY,model_version="frozen-test",
            feature_schema_version=model.RESIDUAL_SCHEMA,code_sha=SHA,policy_version="p",artifact_role="CHALLENGER",
            training_start_ns=1,training_end_ns=2,training_contracts=20,training_days=1,
            assets=("BTC",),contract_templates=("BTC_USD_UPDOWN_5M",),rules_hashes=(RULES,),
            parameters={"calibration_intercept":0.0,"calibration_slope":1.0,
                "feature_names":list(model.RESIDUAL_FEATURES),"feature_means":[0.0]*5,
                "feature_scales":[1.0]*5,"feature_coefficients":[0.0]*5,"standardized_feature_clip":8.0},
            hyperparameters={"protocol":model.PROTOCOL,"automatic_promotion":False,
                "uses_polymarket_price_as_feature":False,"forward_oos_starts_after_ns":2_000_000_000_000,
                "training_market_ids":["prior-market"]},oos_scores={},probability_interval_diagnostics={},economic_replay={},
            generated_timestamp_ns=1_000_000_000_000)

    def rows(self,artifact):
        origin,final=fixture('p',1)
        origin.update(timestamp_ms=2_000_010,external_only_yes=0.8,model_yes=0.8,market_yes=0.295)
        final.update(timestamp_ms=2_400_000,settlement_observed_ms=2_400_000)
        features=model.origin_features(origin)
        common={k:origin[k] for k in ['schema','paper_only','authenticated_execution','real_order_submission',
            'execution_authority','model_sha','model_version','policy_sha256','evidence_semantics_version','market_id']}
        opportunities=[]
        for i,ts in enumerate([2_000_010,2_000_310]):
            books={}
            for side,token,ask,bid in [('YES','y-0',0.30,0.29),('NO','n-0',0.71,0.70)]:
                books[side]={"token_id":token,"asks":[[ask,100.0]],"bids":[[bid,100.0]],
                    "exchange_ts_ms":ts,"receive_ts_ms":ts,"min_order_size":5.0,"tick_size":0.01}
            opportunities.append({**common,'record_id':f'opp-{i}','event_type':'OPPORTUNITY_SET',
                'timestamp_ms':ts,'decision_ts_ms':ts,'reference_version':2000,'contract_rules_hash':RULES,
                'tte_seconds':120.0,'books':books,'fair_yes':0.8,'market_yes':0.295,
                'fee_schedule':{'rate':0.07,'exponent':1,'takerOnly':True},
                'frozen_comparison':{'market_probability':0.295,'structural_probability':0.8,
                    'challenger_probability':0.8,'challenger_hash':artifact.model_hash,
                    'forward_start_ns':artifact.hyperparameters['forward_oos_starts_after_ns'],
                    'frozen_at_ns':artifact.generated_timestamp_ns,'challenger_features':features,'no_money_authority':True}})
        return [origin,final]+opportunities

    def report(self,rows,artifact):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/'tape.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            return model.economic_forward_report([path],artifact,as_of_ms=2_400_001)

    def test_decision_arrival_fees_settlement_and_no_trade_benchmark(self):
        artifact=self.artifact(); report=self.report(self.rows(artifact),artifact)
        self.assertEqual(report['cohorts']['market']['fills'],0)
        for name in ['structural','challenger']:
            cohort=report['cohorts'][name]
            self.assertEqual((cohort['orders'],cohort['fills'],cohort['terminal']),(1,1,1))
            trade=cohort['trades'][0]
            self.assertGreaterEqual(trade['arrival_ms']-trade['decision_ms'],250)
            self.assertLessEqual(trade['entry_debit'],2.0)
            self.assertGreater(trade['fees'],0.0)
            self.assertAlmostEqual(trade['pnl'],-trade['entry_debit'])
            self.assertAlmostEqual(cohort['cash'],4000+trade['pnl'])
        self.assertFalse(report['paper_account_orders_included'])
        self.assertFalse(report['probe_orders_included'])
        self.assertFalse(report['profitability_proven'])

    def test_absent_or_late_arrival_never_invents_fill(self):
        artifact=self.artifact();rows=self.rows(artifact)
        no_arrival=self.report(rows[:-1],artifact)
        self.assertEqual(no_arrival['cohorts']['challenger']['fills'],0)
        self.assertEqual(no_arrival['cohorts']['challenger']['nonfills'],1)
        self.assertEqual(no_arrival['cohorts']['challenger']['cash'],4000)
        rows[-1]['decision_ts_ms']+=5000
        for b in rows[-1]['books'].values():b['exchange_ts_ms']+=5000;b['receive_ts_ms']+=5000
        late=self.report(rows,artifact)
        self.assertEqual(late['cohorts']['challenger']['fills'],0)

    def test_mutated_prediction_or_prepublication_sample_cannot_pass(self):
        artifact=self.artifact();rows=self.rows(artifact)
        rows[-1]['frozen_comparison']['challenger_probability']=0.9
        with self.assertRaisesRegex(ValueError,'frozen_inference_replay_mismatch'):self.report(rows,artifact)
        rows=self.rows(artifact)
        for r in rows:
            if r['event_type']=='OPPORTUNITY_SET':r['reference_version']=1000
        report=self.report(rows,artifact)
        self.assertEqual(report['observation_rows'],0)

    def test_residual_only_enters_monitor_as_forward_shadow(self):
        import v7_rtds_external_fair_monitor as monitor
        artifact=self.artifact();obj=monitor.Monitor.__new__(monitor.Monitor)
        obj.active_market={'contract_start_epoch':2000};obj.active_contract={'normalized_rules_hash':RULES}
        base={'valid':True,'yes':0.8,'structural':0.8,'lower':0.7,'upper':0.9,
              'model_features':{f:0.0 for f in model.RESIDUAL_FEATURES}}
        out=obj.registered_shadow_snapshot(base,artifact,'LOADED','CHALLENGER')
        self.assertTrue(out['explicit_registry_model_applied'])
        self.assertEqual(out['execution_authority'],'SHADOW_ZERO_AUTHORITY')
        self.assertFalse(out['probability_interval_validated'])
        self.assertEqual((out['lower'],out['upper']),(0.0,1.0))
        self.assertEqual(base['yes'],0.8)
        obj.active_market['contract_start_epoch']=1900
        self.assertFalse(obj.registered_shadow_snapshot(base,artifact,'LOADED','CHALLENGER')['valid'])


if __name__=='__main__':unittest.main()
