from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_external_lead_lag_collector import Collector, SCHEMA as OBS_SCHEMA, ORIGIN_SCHEMA, observation_rows
from v7_external_lead_lag_model import validate, predict_probability
from v7_external_lead_lag_train import train, load_rows
from v7_fair_model_artifact import canonical_hash
from v7_compressed_journal import journal_rows
from v7_external_rich_model import FEATURE_SCHEMA
from v7_causal_book import TARGET as BOOK_TARGET

SHA='a'*40


def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')


def router(receive_ms:int,yes:float,snapshot:str)->dict:
    return {'code_sha':SHA,'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'live_market':{'valid':True,
        'source':'LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH','market_id':'m1','yes':yes,
        'snapshot_id':snapshot,'receive_ts_ms':receive_ms,'exchange_ts_ms':receive_ms-1}}


def fair_origin()->dict:
    observed_ns=1_000_000_000
    cut={'observed_wall_ns':observed_ns,'market_probability':.50,
         'market_prior_snapshot':{'valid':True,'snapshot_id':'s0','receive_ts_ms':1000,
                                  'exchange_ts_ms':999},
         'external_features':{},'external_context':{}}
    return {'code_sha':SHA,'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'market':{'market_id':'m1'},'fair':{
            'paper_exploration_learned':True,'market_prior_causal_cut_valid':True,
            'uses_polymarket_price_as_feature':True,'rich_feature_cut':cut,
            'rich_feature_sha256':canonical_hash(cut),
            'rich_model_features':{'ofi':.25,'trade_imbalance':-.1},
            'market_prior_snapshot_id':'s0','pm_mid_receive_ts_ms':1000,
            'pm_mid_exchange_ts_ms':999}}


class LeadLagTests(unittest.TestCase):
    def test_independent_origin_is_stored_once_and_labels_require_exact_source(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);f=fair_origin();cut=f['fair']['rich_feature_cut'];cut['market_id']='m1'
            f['causal_observation']={'schema':'polymarket_v7_model_independent_causal_observation_v1',
                'valid':True,'model_required':False,'cut':cut,'cut_sha256':canonical_hash(cut),
                'features':f['fair']['rich_model_features'],'feature_schema_version':'raw-v1'}
            f['fair']={'valid':False}
            fair=root/'fair.json';route=root/'router.json';tape=root/'leadlag.jsonl'
            write(fair,f);write(route,router(1000,.5,'s0'))
            c=Collector(fair,route,tape,root/'status.json',SHA,maximum_hot_bytes=2048);c.tick()
            for receive in (1100,1250,1500,2000):
                write(route,router(receive,.55,str(receive)));c.tick()
                if c.journal.pending:c.journal.pending.result()
            c.journal.close()
            rows=list(journal_rows(tape))
            self.assertEqual(len(rows),5)
            self.assertEqual(rows[0]['schema'],ORIGIN_SCHEMA)
            self.assertTrue(all('rich_model_features' not in row for row in rows[1:]))
            hydrated=list(observation_rows(rows))
            self.assertEqual(len(hydrated),4)
            self.assertTrue(all(row['rich_feature_cut']==cut for row in hydrated))
            self.assertEqual(len(load_rows([tape],SHA)),4)
            with self.assertRaisesRegex(ValueError,'missing_persisted_origin'):
                list(observation_rows(rows[1:]))
            rows[1]['market_id']='wrong-market'
            with self.assertRaisesRegex(ValueError,'origin_reference_mismatch'):
                list(observation_rows(rows))

    def test_late_snapshots_are_preserved_but_excluded_from_nominal_training(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); fair=root/'fair.json'; route=root/'router.json'; tape=root/'leadlag.jsonl'
            write(fair,fair_origin()); write(route,router(1000,.5,'s0'))
            c=Collector(fair,route,tape,root/'status.json',SHA); c.tick()
            write(route,router(1350,.6,'s1')); c.tick()
            values=[json.loads(x) for x in tape.read_text().splitlines()]
            self.assertEqual(len(values),2)
            self.assertTrue(all(x['label_state']=='LATE_SNAPSHOT_CENSORED' for x in values))
            self.assertEqual(load_rows([tape],SHA),[])
            # Legacy rows must receive the same check, even without the flag.
            for row in values: row.pop('nominal_horizon_eligible')
            tape.write_text(''.join(json.dumps(row)+'\n' for row in values))
            self.assertEqual(load_rows([tape],SHA),[])
            c.publish()
            self.assertEqual(json.loads((root/'status.json').read_text())['late_labels'],2)

    def test_collector_labels_four_receive_time_horizons_without_execution_authority(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); fair=root/'fair.json'; route=root/'router.json'; tape=root/'leadlag.jsonl'; status=root/'status.json'
            write(fair,fair_origin()); write(route,router(1000,.50,'s0'))
            c=Collector(fair,route,tape,status,SHA,25); c.tick()
            self.assertEqual(c.origins,1); self.assertEqual(c.labels,0)
            for receive,yes,sid in ((1100,.51,'s1'),(1250,.52,'s2'),(1500,.54,'s3'),(2000,.57,'s4')):
                write(route,router(receive,yes,sid)); c.tick()
            values=[json.loads(x) for x in tape.read_text().splitlines()]
            self.assertEqual([x['horizon_ms'] for x in values],[100,250,500,1000])
            self.assertEqual(c.labels,4)
            self.assertTrue(all(x['schema']==OBS_SCHEMA for x in values))
            self.assertTrue(all(x['execution_authority']=='ZERO_AUTHORITY_RESEARCH_ONLY' for x in values))
            self.assertTrue(all(x['real_order_submission'] is False for x in values))
            self.assertEqual(values[-1]['label_pm_snapshot_id'],'s4')
            self.assertGreater(values[-1]['delta_logit'],0)

    def test_causal_book_training_reuses_compatible_model_independent_history_across_shas(self):
        with tempfile.TemporaryDirectory() as d:
            tape=Path(d)/'leadlag.jsonl'
            records=[]
            for i,source_sha in enumerate(('b'*40,'c'*40)):
                cut={'market_id':f'm{i}','observed_wall_ns':1_000_000_000+i,
                     'market_probability':.5}
                origin={'schema':ORIGIN_SCHEMA,'origin_id':f'o{i}','market_id':f'm{i}',
                    'model_sha':source_sha,'origin_observed_wall_ns':cut['observed_wall_ns'],
                    'rich_feature_cut':cut,'rich_feature_sha256':canonical_hash(cut),
                    'rich_model_features':{'ofi':.1+i},'feature_schema_version':FEATURE_SCHEMA,
                    'causal_observation_schema':'polymarket_v7_model_independent_causal_observation_v1'}
                ref=canonical_hash(origin); records.append(origin)
                records.append({'schema':OBS_SCHEMA,'paper_only':True,'authenticated_execution':False,
                    'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
                    'model_sha':source_sha,'origin_id':origin['origin_id'],'market_id':origin['market_id'],
                    'origin_observed_wall_ns':origin['origin_observed_wall_ns'],'horizon_ms':500,
                    'realized_horizon_ms':510,'nominal_horizon_eligible':True,
                    'target_semantics':BOOK_TARGET,'delta_logit':.01*(i+1),'origin_record_sha256':ref})
            bad_cut={'market_id':'bad','observed_wall_ns':2_000_000_000,'market_probability':.5}
            bad={'schema':ORIGIN_SCHEMA,'origin_id':'bad','market_id':'bad','model_sha':'e'*40,
                'origin_observed_wall_ns':bad_cut['observed_wall_ns'],'rich_feature_cut':bad_cut,
                'rich_feature_sha256':canonical_hash(bad_cut),'rich_model_features':{'ofi':9.},
                'feature_schema_version':'incompatible-v0',
                'causal_observation_schema':'polymarket_v7_model_independent_causal_observation_v1'}
            records.append(bad)
            records.append({'schema':OBS_SCHEMA,'paper_only':True,'authenticated_execution':False,
                'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
                'model_sha':'e'*40,'origin_id':'bad','market_id':'bad',
                'origin_observed_wall_ns':bad['origin_observed_wall_ns'],'horizon_ms':500,
                'realized_horizon_ms':510,'nominal_horizon_eligible':True,
                'target_semantics':BOOK_TARGET,'delta_logit':9.,'origin_record_sha256':canonical_hash(bad)})
            tape.write_text(''.join(json.dumps(r)+'\n' for r in records))
            rows=load_rows([tape],'d'*40,BOOK_TARGET)
            self.assertEqual(len(rows),2)
            self.assertEqual({r['model_sha'] for r in rows},{'b'*40,'c'*40})
            self.assertEqual({r['market_id'] for r in rows},{'m0','m1'})

    def test_explicit_trainer_builds_frozen_models_on_whole_market_chronology(self):
        rows=[]
        for i in range(40):
            x=(i%9-4)/4
            for h in (100,250,500,1000):
                target=.015*x*(h/100)
                rows.append({'schema':OBS_SCHEMA,'paper_only':True,'authenticated_execution':False,
                    'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
                    'model_sha':SHA,'origin_id':f'o{i}','market_id':f'm{i}','horizon_ms':h,
                    'origin_observed_wall_ns':1_000_000_000+i*300_000_000_000,
                    'realized_horizon_ms':h+10,'delta_logit':target,
                    'rich_model_features':{'ofi':x,'trade_imbalance':.5*x,
                                           'return_100ms_bp':.2*x}})
        model,report=train(rows,SHA); validate(model)
        self.assertEqual(model['training_lifecycle'],'EXPLICIT_FROZEN_ARTIFACT_ONLY')
        self.assertTrue(model['research_only']); self.assertFalse(model['real_order_submission'])
        self.assertEqual(set(model['models']),{'100','250','500','1000'})
        self.assertEqual(model['training_source_model_shas'],[SHA])
        self.assertEqual(report['training_source_model_shas'],[SHA])
        self.assertTrue(all(v['validation_improves_zero_change'] for v in model['models'].values()))
        pred=predict_probability(model,{'ofi':1.,'trade_imbalance':.5,'return_100ms_bp':.2},.5,500)
        self.assertGreater(pred['predicted_delta_logit'],0)
        self.assertIn('500',report['horizons'])

if __name__=='__main__': unittest.main()
