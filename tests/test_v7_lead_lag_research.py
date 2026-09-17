from __future__ import annotations
from dataclasses import replace
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from v7_lead_lag_research import (CausalShock, FeatureObservation, FrozenResidualModel, ResearchRow,
    ShockPolicy, available_features, chronological_split, cluster_mean_ci, connected_clusters,
    fit_residual_model, non_opposing)
from v7_lead_lag_replay import ReplayError, primitive


def observation(value, available=100, unit='log_return_100ms'):
    return FeatureObservation(value, available, available, 'SYNTHETIC', unit, True)


def row(index, *, asset='ETH', decision=None, market=None, shock=None, value=None):
    decision = 100 + index * 100 if decision is None else decision
    x = index / 10 if value is None else value
    return ResearchRow('r' + str(index), asset, 'M5', decision, decision + 10, decision + 11,
                       (x,), .5, .5 + .02 * x, market or 'm' + str(index), shock or 's' + str(index),
                       (decision,), ('SYNTHETIC_SOURCE_' + str(index),))


class CausalResearchTest(unittest.TestCase):
    def test_future_availability_removes_feature(self):
        base = available_features({'flow': observation(.2)}, decision_ns=101, maximum_age_ns={'flow': 10})
        shifted = available_features({'flow': observation(.2, available=102)}, decision_ns=101, maximum_age_ns={'flow': 10})
        self.assertEqual(base['flow']['value'], .2)
        self.assertIsNone(shifted['flow']['value'])
        self.assertEqual(shifted['flow']['reason'], 'FUTURE_INFORMATION')

    def test_future_feature_cannot_be_smuggled_into_training(self):
        with self.assertRaisesRegex(ReplayError, 'NONCAUSAL_FEATURE'):
            replace(row(0), feature_available_ns=(101,))
        with self.assertRaisesRegex(ReplayError, 'NONCAUSAL_FEATURE'):
            replace(row(0), feature_available_ns=(None,))

    def test_feature_stale_missing_and_zero_distinct(self):
        values = {'stale': observation(1., available=50), 'missing': observation(None), 'zero': observation(0.)}
        cut = available_features(values, decision_ns=101, maximum_age_ns={k: 10 for k in values})
        self.assertEqual(cut['stale']['reason'], 'STALE')
        self.assertEqual(cut['missing']['reason'], 'MISSING')
        self.assertEqual((cut['zero']['value'], cut['zero']['missing']), (0., False))

    def test_confirmation_missing_is_not_zero(self):
        a = observation(1.)
        self.assertFalse(non_opposing(a, None, decision_ns=101, maximum_age_ns=10)[0])
        self.assertFalse(non_opposing(a, observation(None), decision_ns=101, maximum_age_ns=10)[0])
        self.assertTrue(non_opposing(a, observation(0.), decision_ns=101, maximum_age_ns=10)[0])
        self.assertFalse(non_opposing(a, observation(-1.), decision_ns=101, maximum_age_ns=10)[0])

    def test_confirmation_rejects_scale_mismatch(self):
        self.assertEqual(non_opposing(observation(1.), observation(1., unit='bp'), decision_ns=101, maximum_age_ns=10)[1], 'CONFIRMATION_UNIT_MISMATCH')

    def test_sigma_uses_history_before_trigger(self):
        tracker = CausalShock(ShockPolicy(10, 100, .001, 2, 100))
        tracker.observe(log_return=.01, available_ns=100, window_ns=10, epoch='a')
        tracker.observe(log_return=.01, available_ns=110, window_ns=10, epoch='a')
        result = tracker.observe(log_return=.1, available_ns=120, window_ns=10, epoch='a')
        self.assertAlmostEqual(result['sigma_before_trigger'], .01)
        self.assertAlmostEqual(result['shock_z'], 10.)

    def test_dense_ticks_do_not_reweight_volatility(self):
        tracker = CausalShock(ShockPolicy(10, 100, .001, 2, 100))
        for t in range(100, 110):
            tracker.observe(log_return=.01, available_ns=t, window_ns=10, epoch='a')
        self.assertEqual(tracker.count, 1)

    def test_gap_epoch_missing_reset_calibration(self):
        for reason in ('gap', 'epoch', 'missing'):
            tracker = CausalShock(ShockPolicy(10, 100, .001, 2, 100))
            tracker.observe(log_return=.01, available_ns=100, window_ns=10, epoch='a')
            tracker.observe(log_return=.01, available_ns=110, window_ns=10, epoch='a')
            result = tracker.observe(log_return=None if reason == 'missing' else .01,
                                     available_ns=1000 if reason == 'gap' else 120,
                                     window_ns=10, epoch='b' if reason == 'epoch' else 'a')
            self.assertIsNone(result['shock_z'])

    def test_duplicate_shock_or_wrong_scale_rejected(self):
        tracker = CausalShock(ShockPolicy(10, 100, .001, 2, 100))
        tracker.observe(log_return=.01, available_ns=100, window_ns=10, epoch='a')
        with self.assertRaises(ReplayError):
            tracker.observe(log_return=.01, available_ns=100, window_ns=10, epoch='a')
        with self.assertRaises(ReplayError):
            tracker.observe(log_return=.01, available_ns=101, window_ns=11, epoch='a')

    def test_chronological_availability_and_embargo(self):
        data = [row(0, decision=100), row(1, decision=195), row(2, decision=215),
                row(3, decision=395), row(4, decision=415)]
        split = chronological_split(data, train_end_ns=200, validation_end_ns=400, embargo_ns=10)
        self.assertEqual([r.row_id for r in split['train']], ['r0'])
        self.assertEqual([r.row_id for r in split['validation']], ['r2'])
        self.assertEqual([r.row_id for r in split['test']], ['r4'])
        self.assertEqual(len(split['purged']), 2)

    def test_market_and_shock_purge_across_assets(self):
        data = [row(0, decision=100, asset='BTC', shock='macro'), row(1, decision=500, asset='ETH', shock='macro'),
                row(2, decision=100, market='common'), row(3, decision=500, asset='SOL', market='common')]
        split = chronological_split(data, train_end_ns=200, validation_end_ns=400, embargo_ns=10)
        self.assertEqual(len(split['train']), 0)
        self.assertEqual(len(split['test']), 2)

    def test_train_only_scaler_and_repricing_target(self):
        model = fit_residual_model([row(i) for i in range(10)], feature_names=('shock',), units=('z',),
                                   training_cutoff_ns=2000, ridge_penalty=.0001, label_horizon_ns=10)
        prediction = model.predict((.7,), feature_names=('shock',), units=('z',), decision_ns=3000, label_horizon_ns=10)
        self.assertAlmostEqual(prediction, .014, places=4)
        self.assertAlmostEqual(model.means[0], .45)
        model.predict((1000.,), feature_names=('shock',), units=('z',), decision_ns=3000, label_horizon_ns=10)
        self.assertAlmostEqual(model.means[0], .45)

    def test_model_future_label_rejected(self):
        with self.assertRaisesRegex(ReplayError, 'LEAKAGE'):
            fit_residual_model([row(0)], feature_names=('x',), units=('z',), training_cutoff_ns=105,
                               ridge_penalty=.1, label_horizon_ns=10)

    def test_model_hash_detects_tampering(self):
        model = fit_residual_model([row(i) for i in range(4)], feature_names=('x',), units=('z',),
                                   training_cutoff_ns=1000, ridge_penalty=.1, label_horizon_ns=10)
        with self.assertRaisesRegex(ReplayError, 'HASH_MISMATCH'):
            replace(model, coefficients=(1., 2., 3.))

    def test_model_json_round_trip_and_immutability(self):
        model = fit_residual_model([row(i) for i in range(4)], feature_names=('x',), units=('z',),
                                   training_cutoff_ns=1000, ridge_penalty=.1, label_horizon_ns=10)
        restored = FrozenResidualModel.from_payload(json.loads(json.dumps(primitive(model))))
        self.assertEqual(restored, model)
        with self.assertRaisesRegex(ReplayError, 'IMMUTABLE'):
            replace(model, means=[0.])

    def test_schema_units_and_horizon_fail_closed(self):
        model = fit_residual_model([row(i) for i in range(4)], feature_names=('x',), units=('z',),
                                   training_cutoff_ns=1000, ridge_penalty=.1, label_horizon_ns=10)
        base = dict(feature_names=('x',), units=('z',), decision_ns=2000, label_horizon_ns=10)
        for changed in ({'units': ('bp',)}, {'feature_names': ('wrong',)}, {'decision_ns': 500}, {'label_horizon_ns': 20}):
            with self.assertRaises(ReplayError):
                model.predict((1.,), **dict(base, **changed))
        with patch('builtins.open', side_effect=AssertionError('INFERENCE FILE I/O')):
            self.assertIsInstance(model.predict((None,), **base), float)

    def test_training_order_deterministic(self):
        rows = [row(i) for i in range(6)]
        args = dict(feature_names=('x',), units=('z',), training_cutoff_ns=1000, ridge_penalty=.1, label_horizon_ns=10)
        self.assertEqual(fit_residual_model(rows, **args), fit_residual_model(list(reversed(rows)), **args))

    def test_all_missing_feature_is_not_synthetic_zero(self):
        with self.assertRaisesRegex(ReplayError, 'UNOBSERVED_TRAINING_FEATURE'):
            fit_residual_model([replace(row(0), features=(None,))], feature_names=('x',), units=('z',),
                               training_cutoff_ns=1000, ridge_penalty=.1, label_horizon_ns=10)

    def test_insufficient_clusters_or_missing_yields_no_ci(self):
        for values, clusters in (([1.] * 100, ['same'] * 100), ([None, 1.], ['a', 'b'])):
            result = cluster_mean_ci(values, clusters, draws=100, minimum_clusters=2)
            self.assertEqual(result['ci95'], [None, None])
            self.assertEqual(result['status'], 'INSUFFICIENT_EVIDENCE')

    def test_partly_missing_cluster_not_counted_complete(self):
        result = cluster_mean_ci([1., None, 2.], ['a', 'a', 'b'], draws=10, minimum_clusters=2)
        self.assertEqual(result['complete_cluster_count'], 1)

    def test_bootstrap_reproducible_ratio_of_sums(self):
        args = dict(draws=100, seed=17, minimum_clusters=2)
        a = cluster_mean_ci([1., 1., -1.], ['a', 'a', 'b'], **args)
        b = cluster_mean_ci([1., 1., -1.], ['a', 'a', 'b'], **args)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a['mean'], 1/3)

    def test_cluster_union_joins_shock_market_and_time(self):
        data = [{'decision_ns': 100, 'market_id': 'btc', 'parent_shock_id': 'macro'},
                {'decision_ns': 200, 'market_id': 'eth', 'parent_shock_id': 'macro'},
                {'decision_ns': 300, 'market_id': 'eth', 'parent_shock_id': 'other'},
                {'decision_ns': 900, 'market_id': 'sol', 'parent_shock_id': 'third'}]
        result = connected_clusters(data, block_ns=50)
        self.assertEqual(result[0], result[1])
        self.assertEqual(result[1], result[2])
        self.assertNotEqual(result[2], result[3])

if __name__ == '__main__':
    unittest.main()
