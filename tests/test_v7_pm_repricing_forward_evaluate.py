import hashlib,json,pathlib,sys,tempfile,unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_pm_repricing_forward_evaluate import evaluate_rows,validate_manifest,FINALIZATION_GRACE_NS
from v7_pm_repricing_incremental_benchmark import SCHEMA,FAMILIES,HORIZONS
SHA='a'*40

def artifact():
    spec=lambda family,name:{'family':family,'feature_names':[name],'means':[0.0],'scales':[1.0],'coefficients':[0.0,0.1,0.0],'ridge':1.0}
    models={}
    for h in HORIZONS:
        models[str(h)]={'PM_MICRO_ONLY':spec('PM_MICRO_ONLY','pm_logit'),
                        'EXTERNAL_ONLY':spec('EXTERNAL_ONLY','ext__return_100ms_bp'),
                        'PM_PLUS_EXTERNAL':spec('PM_PLUS_EXTERNAL','pm__pm_logit')}
    return {'schema':SCHEMA,'code_sha':SHA,'paper_only':True,'authenticated_execution':False,
            'real_order_submission':False,'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY',
            'automatic_promotion':False,'dataset_sha256':'b'*64,'target_semantics':'CAUSAL_BOOK_STATE_AT_HORIZON',
            'window_policy':'ONE_FIXED_EIGHT_HOUR_FORWARD_NO_EARLY_STOPPING','forward_start_ns':10,
            'forward_end_ns':10+8*3_600_000_000_000,'models':models}
class ForwardTests(unittest.TestCase):
    def test_locked_before_fixed_end_has_no_metrics(self):
        a=artifact(); r=evaluate_rows(a,[],a['forward_end_ns']+FINALIZATION_GRACE_NS-1)
        self.assertEqual(r['state'],'LOCKED_UNTIL_FIXED_END'); self.assertIsNone(r['performance_metrics'])
    def test_fixed_end_scores_only_window(self):
        a=artifact(); start=a['forward_start_ns']
        row={'market_id':'m','origin_id':'o','origin_observed_wall_ns':start+1,'horizon_ms':100,
             'delta_logit':.2,'_pm':{'pm_logit':1.0},'_ext':{'return_100ms_bp':1.0}}
        outside={**row,'origin_id':'late','origin_observed_wall_ns':a['forward_end_ns']+1}
        r=evaluate_rows(a,[row,outside],a['forward_end_ns']+FINALIZATION_GRACE_NS)
        self.assertEqual(r['state'],'FINAL_ONE_LOOK'); self.assertEqual(r['forward_rows'],1)
        self.assertEqual(r['forward_markets'],1); self.assertIsNotNone(r['performance_metrics']['100'])
    def test_manifest_binds_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'a.json'; a=artifact(); p.write_text(json.dumps(a))
            h=hashlib.sha256(p.read_bytes()).hexdigest(); m={'schema':'polymarket_v7_pm_repricing_freeze_manifest_v1',
              'artifact_sha256':h,**{k:a[k] for k in ('code_sha','dataset_sha256','forward_start_ns','forward_end_ns')}}
            validate_manifest(m,a,p); p.write_text(json.dumps({**a,'x':1}))
            with self.assertRaisesRegex(ValueError,'artifact_hash'): validate_manifest(m,a,p)

if __name__=='__main__':unittest.main()
