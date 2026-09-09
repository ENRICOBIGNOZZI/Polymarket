import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_evidence_store import EvidenceStore
from v7_permanent_datasets import materialize
from v7_causal_book import TARGET
from v7_external_lead_lag_collector import ORIGIN_SCHEMA
from v7_fair_model_artifact import canonical_hash
from test_v7_profit_attribution import fixture
from test_v7_external_rich_model import origin

class DatasetTests(unittest.TestCase):
    def test_four_views_regenerate_after_source_model_and_index_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);revisions=[]
            canonical=fixture();canonical[0]['metadata'].update(component='professional_maker',placement_action='JOIN',placement_features={'ofi':None})
            o=origin();common=dict(schema='polymarket_v7_external_fair_counterfactual_v1',
                evidence_semantics_version='external-fair-settlement-evidence-v2',paper_only=True,
                authenticated_execution=False,real_order_submission=False,execution_authority='SHADOW_ZERO_AUTHORITY',
                forecast_id='x',market_id='m',model_sha='a'*40,policy_sha256='c'*64)
            o.update(common,event_type='FORECAST',reference_version=1_700_000_100,market_yes=.5,rules_hash='b'*64,
                research_model_model_id='rich-model-A',research_model_model_hash='d'*64,
                yes_token='Y',no_token='N',market_mid_source='LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH',record_id='o')
            f=dict(common,event_type='FORECAST_FINAL',timestamp_ms=1_700_000_401_000,
                settlement_observed_ms=1_700_000_401_000,actual_yes=1.,settlement_closed=True,
                settlement_provider='POLYMARKET_GAMMA_PUBLIC',settlement_token_ids=['Y','N'],
                settlement_outcome_prices=[1.,0.],winning_token_id='Y',record_id='f')
            response={'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
                'origin_id':'response','origin_observed_wall_ns':1000_000_000,'market_id':'m','model_sha':'a'*40,
                'target_semantics':TARGET,'nominal_horizon_eligible':True,'label_state':'CAUSAL_BOOK_OBSERVED',
                'label_target_ts_ms':1100,'label_available_after_receive_ms':1101,
                'label_pm_receive_ts_ms':990,'horizon_ms':100,'realized_horizon_ms':100,
                'yes_token':'Y','no_token':'N','observer_session_id':'session','connection_epoch':1,
                'label_book_cuts':[{'valid':True,'lineage_continuous':True,'market_id':'m','receive_wall_ms':990,
                    'token_id':t,'observer_session_id':'session','connection_epoch':1} for t in ['Y','N']],
                'rich_model_features':{'ofi':None},'origin_pm_yes':.5,'label_pm_yes':.5,'delta_probability':0.}
            paths=[]
            cut={'market_id':'m','observed_wall_ns':response['origin_observed_wall_ns'],'external_features':{'aggregate_ofi':None}}
            persisted_origin={k:response[k] for k in ('paper_only','authenticated_execution','real_order_submission',
                'origin_id','origin_observed_wall_ns','market_id','model_sha')}
            persisted_origin.update(schema=ORIGIN_SCHEMA,rich_feature_cut=cut,rich_feature_sha256=canonical_hash(cut),
                rich_model_features=response.pop('rich_model_features'),feature_schema_version='causal-test-v1')
            response['origin_record_sha256']=canonical_hash(persisted_origin)
            with EvidenceStore(root/'store') as store:
                for family,values in [('canonical_ledger',canonical),('fair_predictions',[o,f]),('lead_lag',[persisted_origin,response])]:
                    path=root/(family+'.jsonl');paths.append(path)
                    path.write_text(''.join(json.dumps(x)+'\n' for x in values))
                    revisions.append(store.capture(path,partition='model-A',relative=path.name,contract={'source_family':family})['revision'])
            first=materialize(root/'store',revisions,root/'datasets',1_800_000_000_000)
            self.assertEqual(first['canonical_reconciliation']['canonical_final_pnl'],'2.27')
            self.assertEqual({k:v['rows'] for k,v in first['datasets'].items()},dict(settlement_prediction=1,pm_response=1,maker_execution=1,economic_decision=1))
            for path in paths:path.unlink()
            for path in (root/'store').glob('index.sqlite*'):path.unlink()
            with EvidenceStore(root/'store') as store:store.rebuild_index()
            second=materialize(root/'store',revisions,root/'regenerated',1_800_000_000_000)
            self.assertEqual(first['manifest_hash'],second['manifest_hash'])
            import gzip
            settlement=json.loads(gzip.decompress((root/'datasets'/first['datasets']['settlement_prediction']['parts'][0]['path']).read_bytes()).splitlines()[0])
            self.assertEqual(settlement['prediction_identities']['research_model_id'],'rich-model-A')
            self.assertEqual(settlement['prediction_identities']['research_model_hash'],'d'*64)
            self.assertEqual(settlement['prediction_identities']['research_model_model_hash'],'d'*64)
            execution=json.loads(gzip.decompress((root/'datasets'/first['datasets']['maker_execution']['parts'][0]['path']).read_bytes()).splitlines()[0])
            self.assertIsNone(execution['opposite_flow_prints'])
            self.assertIsNone(execution['features']['ofi'])
            response=json.loads(gzip.decompress((root/'datasets'/first['datasets']['pm_response']['parts'][0]['path']).read_bytes()).splitlines()[0])
            self.assertTrue(response['fixed_horizon_training_eligible'])  # unchanged valid book predates origin
            self.assertEqual(response['raw_causal_inputs'],cut)
            self.assertEqual(len(response['sources']),2)  # exact label and origin source hashes

if __name__=='__main__':unittest.main()
