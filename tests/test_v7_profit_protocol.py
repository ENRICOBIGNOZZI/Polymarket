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
