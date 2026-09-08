from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_external_lead_lag_collector import Collector, SCHEMA as OBS_SCHEMA
from v7_external_lead_lag_model import validate, predict_probability
from v7_external_lead_lag_train import train
from v7_fair_model_artifact import canonical_hash

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
        self.assertTrue(all(v['validation_improves_zero_change'] for v in model['models'].values()))
        pred=predict_probability(model,{'ofi':1.,'trade_imbalance':.5,'return_100ms_bp':.2},.5,500)
        self.assertGreater(pred['predicted_delta_logit'],0)
        self.assertIn('500',report['horizons'])

if __name__=='__main__': unittest.main()
