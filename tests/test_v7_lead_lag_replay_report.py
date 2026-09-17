from __future__ import annotations
import copy
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_v7_lead_lag_replay import fixture, proof, T, W
from v7_lead_lag_replay import ReplayError, primitive
from v7_lead_lag_replay_report import build_report, source_manifest, validate_dataset


def dataset():
    i, c, s = fixture()
    hashes = {k: 'd' * 64 for k in ('config_hash', 'feature_schema_hash', 'model_hash',
                                   'fill_model_hash', 'cost_model_hash', 'settlement_semantic_hash')}
    identity = dict(code_sha='1' * 40, experiment_id=i.experiment_id, protocol_hash=i.protocol_hash,
                    latency_profile_id=s.profile_id, run_id='SYNTHETIC_RUN', data_cutoff_wall_ns=W-1, **hashes)
    policy = dict(processing_ns=0, network_ns=5_000_000, depth_fraction='1', block_ns=60_000_000_000,
                  minimum_clusters=2, bootstrap_draws=50, bootstrap_seed=17, require_initial_full_depth=True,
                  currency='USDC', shared_cash='100', portfolio_loss_cap='100', asset_loss_cap='100',
                  horizon_loss_cap='100', shock_loss_cap='100', modeled_redemption_delay_ns=0)
    return {'schema': 'polymarket_v7_lead_lag_replay_dataset_v1', 'evidence_type': 'SYNTHETIC',
            'identity': identity, 'source_files': [], 'candidates': [primitive(i)], 'cuts': [primitive(c)],
            'resolutions': [primitive(proof())], 'as_of_wall_ns': W+200_000_000_000, 'analysis_policy': policy}


def report(raw, latencies=(0, 5, 1000), sizes=(D(5), D(25))):
    return build_report(raw, latencies_ms=latencies, sizes=sizes, verified_sources=[], report_code_sha='2'*40)


def add_asset(raw, asset='SOL', *, delta_ns=0, matching_delay=0):
    i, c, s = fixture(asset, market='market-'+asset, token='yes-'+asset)
    i = replace(i, clock=i.clock.after(delta_ns), signal_monotonic_ns=T+delta_ns,
                expires_monotonic_ns=T+delta_ns+2_000_000_000,
                signal_expires_monotonic_ns=T+delta_ns+5_000_000_000, matching_delay_ns=matching_delay)
    c = replace(c, available_monotonic_ns=T+delta_ns, matching_delay_ns=matching_delay,
                book=replace(c.book, clock=c.book.clock.after(delta_ns), transport_monotonic_ns=T+delta_ns))
    raw['candidates'].append(primitive(i))
    raw['cuts'].append(primitive(c))
    raw['resolutions'].append(primitive(proof(i.market_id, i.token_id)))


class ReplayReportTest(unittest.TestCase):
    def test_replay_is_deterministic_and_input_not_mutated(self):
        raw = dataset()
        original = copy.deepcopy(raw)
        self.assertEqual(report(raw), report(raw))
        self.assertEqual(raw, original)

    def test_all_real_authority_flags_off(self):
        result = report(dataset())
        self.assertTrue(result['paper_only'])
        for key in ('authenticated_execution', 'real_order_submission', 'real_capital_at_risk', 'entry_authority', 'automatic_promotion'):
            self.assertFalse(result[key])
        self.assertEqual(result['economic_evidence'], 'NOT_PROVEN')
        self.assertEqual(result['execution_evidence'], 'SIMULATED')

    def test_latency_unknown_not_zero_pnl(self):
        result = report(dataset())
        first, _, last = result['latency_scenarios']
        self.assertEqual(D(first['total_pnl']), D('2.41250'))
        self.assertIsNone(last['total_pnl'])
        self.assertIsNone(last['mean_pnl_per_candidate'])
        self.assertEqual(last['unknown_candidates'], 1)
        self.assertGreater(D(last['reserved_cash_as_of_report']), D(0))

    def test_capacity_keeps_initial_depth_gate_and_denominator(self):
        result = report(dataset())
        small, large = result['capacity_scenarios']
        self.assertEqual(small['candidate_count'], large['candidate_count'])
        self.assertEqual(large['reason_counts'], {'INITIAL_DEPTH_INSUFFICIENT': 1})
        self.assertEqual(D(large['total_pnl']), D(0))
        self.assertEqual(large['counterfactual_quantity'], '25')

    def test_unresolved_payout_not_zero_and_cash_stays_committed(self):
        raw = dataset()
        raw['resolutions'] = []
        result = report(raw)['latency_scenarios'][0]
        self.assertIsNone(result['total_pnl'])
        self.assertEqual(D(result['cash_as_of_report']), D('97.41250'))
        self.assertEqual(result['unredeemed_positions'], 1)

    def test_redemption_updates_cash_but_is_explicitly_modeled(self):
        result = report(dataset())['latency_scenarios'][0]
        self.assertEqual(D(result['cash_as_of_report']), D('102.41250'))
        self.assertEqual(result['unredeemed_positions'], 0)

    def test_redemption_delay_prevents_early_capital_reuse(self):
        raw = dataset()
        raw['analysis_policy']['modeled_redemption_delay_ns'] = 100_000_000_000
        result = report(raw)['latency_scenarios'][0]
        self.assertEqual(D(result['cash_as_of_report']), D('97.41250'))
        self.assertEqual(result['unredeemed_positions'], 1)
        self.assertEqual(D(result['total_pnl']), D('2.41250'))

    def test_decision_time_cash_reservations_shared_between_assets(self):
        raw = dataset()
        add_asset(raw)
        raw['analysis_policy']['shared_cash'] = '3'
        result = report(raw, latencies=(0,), sizes=(D(5),))['latency_scenarios'][0]
        self.assertEqual(result['filled_candidates'], 1)
        self.assertEqual(result['reason_counts'].get('GLOBAL_CASH_LIMIT'), 1)

    def test_slow_order_reserves_cash_before_faster_later_arrival(self):
        raw = dataset()
        raw['candidates'][0]['matching_delay_ns'] = 100_000_000
        raw['cuts'][0]['matching_delay_ns'] = 100_000_000
        add_asset(raw, delta_ns=1_000_000)
        raw['analysis_policy']['shared_cash'] = '3'
        result = report(raw, latencies=(0,), sizes=(D(5),))['latency_scenarios'][0]
        by_asset = {r['asset']: r for r in result['rows']}
        self.assertEqual(by_asset['ETH']['status'], 'FILLED')
        self.assertEqual(by_asset['SOL']['reason'], 'GLOBAL_CASH_LIMIT')

    def test_shared_shock_limit_not_six_independent_budgets(self):
        raw = dataset()
        add_asset(raw)
        raw['analysis_policy']['shock_loss_cap'] = '4'
        result = report(raw, latencies=(0,), sizes=(D(5),))['latency_scenarios'][0]
        self.assertEqual(result['filled_candidates'], 1)
        self.assertEqual(result['reason_counts'].get('RISK_LIMIT:parent_shock_id'), 1)

    def test_one_market_reservation_and_partial_fill_burn_entry(self):
        raw = dataset()
        other = copy.deepcopy(raw['candidates'][0])
        other['trace_id'], other['signal_id'] = 'trace-z', 'signal-z'
        raw['candidates'].append(other)
        result = report(raw, latencies=(0,), sizes=(D(5),))['latency_scenarios'][0]
        self.assertEqual(result['filled_candidates'], 1)
        self.assertEqual(result['reason_counts'].get('MARKET_ALREADY_RESERVED_OR_TRADED'), 1)

    def test_unknown_execution_blocks_new_decisions_without_freeing_cash(self):
        raw = dataset()
        raw['candidates'][0]['max_transport_age_ns'] = 1
        add_asset(raw, delta_ns=10_000_000)
        result = report(raw, latencies=(0,), sizes=(D(5),))['latency_scenarios'][0]
        self.assertEqual(result['reason_counts'].get('PRIOR_EXECUTION_UNKNOWN'), 1)
        self.assertGreater(D(result['reserved_cash_as_of_report']), D(0))
        self.assertEqual(D(result['cash_as_of_report']), D(100))

    def test_arrival_after_report_cutoff_stays_pending(self):
        raw = dataset()
        raw['as_of_wall_ns'] = W + 1_000_000
        result = report(raw, latencies=(0,), sizes=(D(5),))['latency_scenarios'][0]
        self.assertEqual(result['reason_counts'], {'ARRIVAL_PENDING_AT_REPORT_CUTOFF': 1})
        self.assertIsNone(result['total_pnl'])
        self.assertGreater(D(result['reserved_cash_as_of_report']), D(0))

    def test_latency_can_improve_pnl_by_not_filling_a_loser(self):
        raw = dataset()
        raw['resolutions'] = [primitive(proof(winner=False))]
        changed = copy.deepcopy(raw['cuts'][0])
        changed['available_monotonic_ns'] = T + 10_000_000
        changed['book']['snapshot_id'] = 'late-book'
        changed['book']['clock']['monotonic_ns'] += 10_000_000
        changed['book']['clock']['wall_ns'] += 10_000_000
        changed['book']['transport_monotonic_ns'] += 10_000_000
        changed['book']['asks'] = [['.60', '20']]
        raw['cuts'].append(changed)
        a, b = report(raw, latencies=(0, 20), sizes=(D(5),))['latency_scenarios']
        self.assertLess(D(a['total_pnl']), D(b['total_pnl']))
        self.assertEqual(a['candidate_count'], b['candidate_count'])
        self.assertEqual(D(b['total_pnl']), D(0))

    def test_full_lineage_required(self):
        raw = dataset()
        raw['identity'].pop('fill_model_hash')
        with self.assertRaisesRegex(ReplayError, 'LINEAGE_INCOMPLETE'):
            report(raw)

    def test_mixed_protocol_and_duplicate_attempt_rejected(self):
        raw = dataset()
        raw['candidates'][0]['protocol_hash'] = 'f'*64
        with self.assertRaisesRegex(ReplayError, 'MIXED_EXPERIMENT'):
            report(raw)
        raw = dataset()
        raw['candidates'].append(copy.deepcopy(raw['candidates'][0]))
        with self.assertRaisesRegex(ReplayError, 'NOT_DEDUPLICATED'):
            report(raw)

    def test_mixed_currency_rejected(self):
        raw = dataset()
        raw['cuts'][0]['fee']['currency'] = 'USD'
        with self.assertRaisesRegex(ReplayError, 'MIXED_COLLATERAL'):
            report(raw)

    def test_clock_step_or_multiple_boot_rejected(self):
        for key in ('wall_ns', 'boot_id'):
            raw = dataset()
            add_asset(raw)
            if key == 'wall_ns':
                raw['candidates'][1]['clock'][key] += 1
            else:
                raw['candidates'][1]['clock'][key] = 'new-boot'
            with self.assertRaises(ReplayError):
                report(raw)

    def test_duplicate_resolution_is_not_last_write_wins(self):
        raw = dataset()
        raw['resolutions'].append(copy.deepcopy(raw['resolutions'][0]))
        with self.assertRaisesRegex(ReplayError, 'EXPLICIT_CORRECTION'):
            report(raw)

    def test_source_hash_verification_and_freeze_tampering(self):
        raw = dataset()
        raw['evidence_type'] = 'RECORDED'
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base/'source.jsonl').write_text('explicitly synthetic source for hash test\n')
            h = hashlib.sha256((base/'source.jsonl').read_bytes()).hexdigest()
            raw['source_files'] = [{'path': 'source.jsonl', 'sha256': h}]
            self.assertEqual(source_manifest(raw, base=base)[0]['sha256'], h)
            (base/'source.jsonl').write_text('changed after freeze')
            with self.assertRaisesRegex(ReplayError, 'SOURCE_HASH_MISMATCH'):
                source_manifest(raw, base=base)

    def test_recorded_claim_without_verified_sources_rejected(self):
        raw = dataset()
        raw['evidence_type'] = 'RECORDED'
        with self.assertRaisesRegex(ReplayError, 'SOURCES_NOT_VERIFIED'):
            report(raw)

    def test_report_hash_changes_when_policy_changes(self):
        raw = dataset()
        a = report(raw)
        raw['analysis_policy']['depth_fraction'] = '.5'
        b = report(raw)
        self.assertNotEqual(a['report_hash'], b['report_hash'])
        self.assertNotEqual(a['analysis_policy_hash'], b['analysis_policy_hash'])

    def test_cli_executes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, output = base/'input.json', base/'report.json'
            source.write_text(json.dumps(dataset()))
            command = [sys.executable, str(ROOT/'scripts/v7_lead_lag_replay_report.py'), '--input', str(source),
                       '--output', str(output), '--report-code-sha', '2'*40, '--latencies-ms', '0,5', '--sizes', '5,25']
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output.read_bytes()
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), original)

if __name__ == '__main__':
    unittest.main()
