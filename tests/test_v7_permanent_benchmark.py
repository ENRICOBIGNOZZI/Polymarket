import copy
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import v7_permanent_benchmark as module
from v7_external_rich_model import FEATURE_NAMES


def rows():
    return [{'market_id': str(i), 'market_start_ms': i*300000, 'observed_ms': i*300000+1000,
             'label_received_ms': (i+1)*300000+1000, 'actual': float(i % 2),
             'market_probability': .5, 'features': {FEATURE_NAMES[0]: float(i % 2)},
             'stored_predictions': {'rich_research': .5, 'structural': None, 'hybrid': .5}}
            for i in range(120)]


class BenchmarkTests(unittest.TestCase):
    def test_contracts_and_label_availability_never_cross_folds(self):
        source=rows();source.extend(copy.deepcopy(source[:10]))
        development, audit, folds=module.partitions(source)
        audit_ids={r['market_id'] for r in audit};start=min(r['market_start_ms'] for r in audit)
        self.assertFalse(audit_ids & {r['market_id'] for r in development})
        self.assertTrue(all(r['label_received_ms'] < start for r in development))
        for train, validation in folds:
            self.assertFalse({r['market_id'] for r in train} & {r['market_id'] for r in validation})
            self.assertLess(max(r['label_received_ms'] for r in train),min(r['market_start_ms'] for r in validation))

    def test_audit_outcomes_and_extreme_features_cannot_change_frozen_candidate(self):
        source=rows(); altered=copy.deepcopy(source)
        for row in altered[96:]:
            row['actual']=1-row['actual'];row['features'][FEATURE_NAMES[0]]=1e12
        frozen=[]
        def freeze(candidate):
            frozen.append(candidate);return module.digest(module.canonical(candidate))
        real_fit=module.fit; fitted=[]
        def checked_fit(data, ridge, offset):
            self.assertTrue(all(int(r['market_id']) < 96 for r in data))
            parameters=real_fit(data,ridge,offset);fitted.append(parameters)
            self.assertLess(max(map(abs,parameters['means'])),2)
            return parameters
        with mock.patch.object(module,'fit',side_effect=checked_fit):
            a=module.benchmark(source,freeze);b=module.benchmark(altered,freeze)
        self.assertGreater(len(fitted),0)
        self.assertEqual(a['frozen_candidate_hash'],b['frozen_candidate_hash'])
        self.assertEqual(frozen[0],frozen[1])
        self.assertFalse(a['promotion_allowed'])
        self.assertEqual(a['audit']['stored_models']['structural']['contracts'],0)
        self.assertIsNone(a['audit']['stored_models']['structural']['brier'])

    def test_repeated_snapshots_do_not_inflate_contract_score(self):
        source=rows()[:2];source[0]['stored_predictions']['hybrid']=0
        before=module.stored_score(source,'hybrid')
        after=module.stored_score(source+[copy.deepcopy(source[0])]*100,'hybrid')
        self.assertEqual(before['brier'],after['brier'])
        self.assertEqual(after['contracts'],2)


if __name__=='__main__':unittest.main()
