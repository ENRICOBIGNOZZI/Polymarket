import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_profit_protocol import freeze, validate, bin_index, FIVE_MIN_NS

class ProtocolTests(unittest.TestCase):
    def setUp(self):self.protocol=json.loads((ROOT/'config/v7_profit_experiment.json').read_text())

    def test_registration_precedes_forward_boundary_and_cannot_be_reselected(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'manifest.json';manifest=freeze(path,self.protocol,'a'*40,'b'*64,300_000_000_001)
            self.assertEqual(manifest['forward_start_ns'],600_000_000_000)
            self.assertEqual(freeze(path,self.protocol,'a'*40,'b'*64,900_000_000_000),manifest)
            changed=copy.deepcopy(self.protocol);changed['signal']['margin_edges'][1]=.015
            with self.assertRaisesRegex(ValueError,'immutable_identity_changed'):
                freeze(path,changed,'a'*40,'b'*64,900_000_000_000)
            tampered=dict(manifest,forward_start_ns=1);path.write_text(json.dumps(tampered))
            with self.assertRaisesRegex(ValueError,'manifest_hash_mismatch'):
                freeze(path,self.protocol,'a'*40,'b'*64,900_000_000_000)

    def test_new_confirmatory_window_is_exactly_eight_hours(self):
        with tempfile.TemporaryDirectory() as d:
            manifest=freeze(Path(d)/"manifest.json",self.protocol,"a"*40,"b"*64,300_000_000_001)
            self.assertEqual(manifest["confirmatory_end_ns"]-manifest["forward_start_ns"],8*3_600_000_000_000)

    def test_explicit_replication_start_is_boundary_locked_and_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"manifest.json"
            start=4*FIVE_MIN_NS
            manifest=freeze(path,self.protocol,"a"*40,"b"*64,300_000_000_001,
                            cohort={"residual_replication_index":1},forward_start_ns=start)
            self.assertEqual(manifest["forward_start_ns"],start)
            self.assertEqual(freeze(path,self.protocol,"a"*40,"b"*64,999,
                                    cohort={"residual_replication_index":1},forward_start_ns=start),manifest)
            with self.assertRaisesRegex(ValueError,"immutable_forward_start_changed"):
                freeze(path,self.protocol,"a"*40,"b"*64,999,
                       cohort={"residual_replication_index":1},forward_start_ns=start+FIVE_MIN_NS)
            with self.assertRaisesRegex(ValueError,"forward_start_boundary"):
                freeze(Path(d)/"bad.json",self.protocol,"a"*40,"b"*64,999,
                       forward_start_ns=start+1)

    def test_legacy_seven_day_protocol_remains_readable(self):
        legacy=copy.deepcopy(self.protocol)
        legacy["protocol_id"]="permanent-profit-causes-20260909-v3"
        legacy["inference"]["bootstrap_seed"]=20260908
        confirm=legacy["inference"]["confirmatory"]
        confirm.pop("duration_hours")
        confirm.pop("censoring",None)
        confirm["calendar_days"]=7
        validate(legacy)
        with tempfile.TemporaryDirectory() as d:
            manifest=freeze(Path(d)/"manifest.json",legacy,"a"*40,"b"*64,300_000_000_001)
            self.assertEqual(manifest["confirmatory_end_ns"]-manifest["forward_start_ns"],7*86_400_000_000_000)

    def test_v6_censoring_identity_is_fail_fast(self):
        validate(self.protocol)
        for mutation in ('cap','mode','missing'):
            value=copy.deepcopy(self.protocol);c=value['inference']['confirmatory']['censoring']
            if mutation=='cap':c['endpoint_max_censor_fraction']['selected_settlement_surplus_cost2_delay1000']=.051
            elif mutation=='mode':c['mode']='OTHER'
            else:c['terminal_records_required']=False
            with self.assertRaisesRegex(ValueError,'confirmatory_censoring_identity'):validate(value)
        legacy=copy.deepcopy(self.protocol);legacy['protocol_id']='legacy-with-v6-fields';legacy['inference']['bootstrap_seed']=20260908
        with self.assertRaisesRegex(ValueError,'censoring_requires_registered_protocol'):validate(legacy)

    def test_v6_requires_residual_model_and_three_same_artifact_replications(self):
        validate(self.protocol)
        for mutation in ('prefix','count','retrain','pool'):
            value=copy.deepcopy(self.protocol);confirm=value['inference']['confirmatory']
            if mutation=='prefix':value['signal']['required_probability_model_id_prefix']='btc-m5-rich-logit-'
            elif mutation=='count':confirm['replication_count']=2
            elif mutation=='retrain':confirm['retraining_between_replications']=True
            else:confirm['replication_results_pooled_for_primary_claim']=True
            with self.assertRaisesRegex(ValueError,'v6_(residual_model|replication)_identity'):validate(value)

    def test_protocol_cannot_enable_trading_or_change_owner(self):
        for change in ('authority','post_only','promotion'):
            value=copy.deepcopy(self.protocol)
            if change=='authority':value['real_order_submission']=True
            elif change=='post_only':value['maker']['post_only_required']=False
            else:value['inference']['automatic_promotion']=True
            with self.assertRaises(ValueError):validate(value)
        self.assertEqual(bin_index(.01,[0,.01,.03,1]),1)
        self.assertIsNone(bin_index(-.01,[0,.01,.03,1]))

if __name__=='__main__':unittest.main()
