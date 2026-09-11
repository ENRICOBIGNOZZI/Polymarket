import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_profit_protocol import freeze, validate, bin_index

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

    def test_legacy_seven_day_protocol_remains_readable(self):
        legacy=copy.deepcopy(self.protocol)
        legacy["protocol_id"]="permanent-profit-causes-20260909-v3"
        confirm=legacy["inference"]["confirmatory"]
        confirm.pop("duration_hours")
        confirm.pop("censoring",None)
        confirm["calendar_days"]=7
        validate(legacy)
        with tempfile.TemporaryDirectory() as d:
            manifest=freeze(Path(d)/"manifest.json",legacy,"a"*40,"b"*64,300_000_000_001)
            self.assertEqual(manifest["confirmatory_end_ns"]-manifest["forward_start_ns"],7*86_400_000_000_000)

    def test_v5_censoring_identity_is_fail_fast(self):
        validate(self.protocol)
        for mutation in ('cap','mode','missing'):
            value=copy.deepcopy(self.protocol);c=value['inference']['confirmatory']['censoring']
            if mutation=='cap':c['endpoint_max_censor_fraction']['selected_settlement_surplus_cost2_delay1000']=.051
            elif mutation=='mode':c['mode']='OTHER'
            else:c['terminal_records_required']=False
            with self.assertRaisesRegex(ValueError,'v5_censoring_identity'):validate(value)
        legacy=copy.deepcopy(self.protocol);legacy['protocol_id']='legacy-with-v5-fields'
        with self.assertRaisesRegex(ValueError,'censoring_requires_v5'):validate(legacy)

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
