#!/usr/bin/env python3
from __future__ import annotations
import copy
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import v7_multi_crypto_feature_engine as feature_engine_module
from v7_multi_crypto_feature_engine import ASSETS, FeatureEngine, ShockTracker, validate_policy

SHA = 'a' * 40
NOW = 2_000_000_000_000_000_000


def policy() -> dict:
    return json.loads((ROOT / 'config/v7_multi_crypto_feature_policy.json').read_text())


def external(asset: str, *, timestamp_ns: int = NOW - 10_000_000, version: int = 1) -> dict:
    return {
        'paper_only': True, 'authenticated_execution': False, 'real_order_submission': False,
        'code_sha': SHA, 'asset': asset, 'timestamp_ns': timestamp_ns, 'valid': True,
        'state_version': version, 'latest_input_receive_monotonic_ns': version * 100_000_000,
        'composite_price': 100.0 + ASSETS.index(asset), 'composite_microprice': 100.01,
        'return_50ms': None, 'return_100ms': 0.001, 'return_250ms': 0.002, 'return_1s': 0.003,
        'dispersion_bps': 1.0, 'aggregate_ofi': 0.2, 'aggregate_trade_imbalance': -0.1,
        'fresh_venue_count': 3,
        'derivative_contexts': [{
            'venue': 'BINANCE_USDM', 'healthy': True, 'valid_mask': 7, 'age_ns': 10_000_000,
            'mark_price': 100.1, 'index_price': 100.0, 'funding_rate': 0.0001,
            'open_interest_native': 123.0,
        }],
    }


def all_external() -> dict[str, dict]:
    return {asset: external(asset) for asset in ASSETS}


def selection() -> dict:
    return {
        'schema': 'polymarket_v7_multi_crypto_book_selection_v1', 'model_sha': SHA,
        'paper_only': True, 'authenticated_execution': False, 'real_order_submission': False,
        'execution_authority': False, 'generated_at_ms': NOW // 1_000_000 - 20,
        'markets': [{
            'asset': 'ETH', 'horizon': 'M5', 'market_id': 'm1', 'event_id': 'e1',
            'yes_token': 'yes1', 'no_token': 'no1', 'normalized_rules_hash': 'r' * 64,
            'start_timestamp': '2033-05-18T03:30:00Z', 'end_timestamp': '2033-05-18T03:35:00Z',
        }],
    }


def oracle() -> dict:
    assets = {asset: {'fresh': True, 'receive_age_ms': 10.0, 'price': 100.0 + ASSETS.index(asset),
                      'version': 7, 'source_timestamp_ms': NOW // 1_000_000 - 20,
                      'receive_wall_ns': NOW - 10_000_000}
              for asset in ASSETS}
    return {
        'model_sha': SHA, 'timestamp_ns': NOW - 1_000_000, 'state': 'RUNNING', 'paper_only': True, 'authenticated_execution': False,
        'real_order_submission': False, 'execution_authority': False, 'assets': assets,
        'settlement_references': {'m1': {
            'asset': 'ETH', 'horizon': 'M5', 'market_id': 'm1', 'valid': True,
            'price': 100.5, 'normalized_rules_hash': 'r' * 64,
            'source_timestamp_ms': NOW // 1_000_000 - 30, 'captured_at_ms': NOW // 1_000_000 - 15,
            'available_wall_ns': NOW - 15_000_000,
        }},
    }


def write_books(directory: Path, *, yes_wall_ms: int | None = None, no_wall_ms: int | None = None) -> None:
    default = (NOW - 5_000_000) // 1_000_000
    for token, bid, ask, wall in (
        ('yes1', 0.49, 0.51, yes_wall_ms if yes_wall_ms is not None else default),
        ('no1', 0.48, 0.52, no_wall_ms if no_wall_ms is not None else default),
    ):
        (directory / f'{token}.json').write_text(json.dumps({
            'model_sha': SHA, 'market_id': 'm1', 'token_id': token,
            'paper_only': True, 'authenticated_execution': False, 'real_order_submission': False,
            'execution_authority': 'ZERO_AUTHORITY_RESEARCH_ONLY', 'valid': True,
            'lineage_continuous': True, 'receive_wall_ms': wall, 'state_version': 9, 'connection_epoch': 2,
            'best_bid': bid, 'best_ask': ask, 'placement_features': {'imbalance': 0.1},
        }))


def build(ext=None, ora=None, sel=None, *, yes_wall_ms=None, capture_clock=None,
          contract_state=None):
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp); write_books(d, yes_wall_ms=yes_wall_ms)
        engine = FeatureEngine(policy())
        out = engine.build(external=ext or all_external(), oracle=ora or oracle(),
                           selection=sel or selection(), book_dir=d, model_sha=SHA,
                           now_ns=NOW, capture_clock=capture_clock,
                           contract_state=contract_state)
        return engine, out


def contract_state(state='ACTIVE_READY_SHADOW', *, available_at_ns=NOW - 1):
    return {
        'schema': 'polymarket_v7_multi_crypto_contract_state_v1',
        'model_sha': SHA, 'paper_only': True, 'authenticated_execution': False,
        'real_order_submission': False, 'execution_authority': False,
        'markets': [{
            'market_id': 'm1', 'state': state, 'entry_authority': False,
            'available_at_ns': available_at_ns, 'contract_state_hash': 'e' * 64,
        }],
    }


def test_missing_feature_is_not_zero_and_shadow_never_signals() -> None:
    _, out = build()
    row = out['markets'][0]
    assert row['external']['return_50ms_bp'] is None
    assert row['external']['return_100ms_bp'] == 10.0
    assert row['signal_eligible'] is False
    assert row['blockers'] == ['UNCALIBRATED_SHADOW']
    assert row['active_now'] is True
    assert len(row['feature_schema_hash']) == 64
    assert len(row['source_identity_hash']) == 64
    assert row['available_at_ns'] <= NOW
    assert out['ready_for_calibration_markets'] == 1
    assert len(out['policy_hash']) == 64 and len(out['feature_schema_hash']) == 64


def test_future_or_stale_external_never_updates_shock() -> None:
    ext = all_external(); ext['ETH']['timestamp_ns'] = NOW + 1
    engine, out = build(ext=ext)
    row = out['markets'][0]
    assert row['external']['fresh'] is False
    assert row['external']['composite_price'] is None
    assert row['external']['shock']['return_100ms_bp'] is None
    assert engine.shocks['ETH'].observations == 0
    assert 'EXTERNAL_FEED_STALE_OR_IDENTITY_INVALID' in row['blockers']



def test_decision_cut_is_assigned_after_book_snapshot_reads() -> None:
    original = feature_engine_module.time.time_ns
    feature_engine_module.time.time_ns = lambda: NOW + 10_000_000
    try:
        _, out = build(yes_wall_ms=(NOW + 5_000_000) // 1_000_000,
                       capture_clock=feature_engine_module.time.time_ns)
    finally:
        feature_engine_module.time.time_ns = original
    row = out['markets'][0]
    assert out['timestamp_ns'] == NOW + 10_000_000
    assert row['pm_book_valid'] is True
    assert row['pm_yes_age_ms'] == 5.0
    assert 'FUTURE_OR_UNKNOWN_INPUT_AVAILABILITY' not in row['blockers']

def test_future_book_timestamp_is_not_causal() -> None:
    _, out = build(yes_wall_ms=NOW // 1_000_000 + 1)
    row = out['markets'][0]
    assert row['pm_book_valid'] is False
    assert row['pm_yes_age_ms'] is None
    assert 'PM_BOOK_INVALID_OR_STALE' in row['blockers']


def test_reference_lineage_mismatch_fails_closed() -> None:
    ora = oracle(); ora['settlement_references']['m1']['normalized_rules_hash'] = 'x' * 64
    _, out = build(ora=ora)
    row = out['markets'][0]
    assert row['reference_valid'] is False
    assert row['reference_price'] is None
    assert 'MISSING_OR_MISMATCHED_REFERENCE' in row['blockers']


def test_identity_mismatch_is_rejected() -> None:
    ora = oracle(); ora['model_sha'] = 'b' * 40
    try:
        build(ora=ora)
    except ValueError as exc:
        assert 'oracle source authority/identity invalid' in str(exc)
    else:
        raise AssertionError('wrong SHA accepted')


def test_shock_uses_prior_sigma_and_duplicate_version_does_not_relearn() -> None:
    tracker = ShockTracker(half_life_seconds=60.0, minimum_observations=2)
    def row(version: int, fraction: float):
        return {'state_version': version, 'latest_input_receive_monotonic_ns': version * 100_000_000,
                'return_100ms': fraction}
    assert tracker.update(row(1, .001))['shock_z_unfloored'] is None
    assert tracker.update(row(2, .001))['shock_z_unfloored'] is None
    third = tracker.update(row(3, .01))
    assert math.isclose(third['sigma_100ms_bp_prior'], 10.0, rel_tol=1e-9)
    assert math.isclose(third['shock_z_unfloored'], 10.0, rel_tol=1e-9)
    before = tracker.observations
    tracker.update(row(3, .50))
    assert tracker.observations == before


def test_policy_cannot_sneak_in_calibration() -> None:
    bad = policy(); bad['sigma_floor_bp'] = 0.1
    try:
        validate_policy(bad)
    except ValueError as exc:
        assert 'cannot invent floor/threshold' in str(exc)
    else:
        raise AssertionError('calibrated floor accepted in shadow policy')



def test_imported_engine_is_from_this_exact_checkout() -> None:
    module = sys.modules[FeatureEngine.__module__]
    assert Path(module.__file__).resolve().parent == (ROOT / 'scripts').resolve()


def test_duplicate_shock_returns_identical_prior_sigma() -> None:
    tracker = ShockTracker(half_life_seconds=60, minimum_observations=2)
    def item(v, r):
        return {'state_version': v, 'latest_input_receive_monotonic_ns': v * 100_000_000, 'return_100ms': r}
    tracker.update(item(1, .001)); tracker.update(item(2, .001))
    original = tracker.update(item(3, .01))
    assert tracker.update(item(3, .01)) == original
    assert tracker.update(item(3, .50))['shock_z_unfloored'] is None
    assert tracker.update(item(2, .001))['shock_z_unfloored'] is None
    assert tracker.observations == 3


def test_cached_fresh_flag_does_not_refresh_old_oracle_data() -> None:
    ora = oracle(); ora['assets']['ETH']['receive_wall_ns'] = NOW - 60_000_000_000
    _, out = build(ora=ora)
    assert out['markets'][0]['oracle_fresh'] is False
    assert out['markets'][0]['oracle_price'] is None


def test_future_oracle_snapshot_is_not_available() -> None:
    ora = oracle(); ora['timestamp_ns'] = NOW + 1
    _, out = build(ora=ora)
    assert out['markets'][0]['oracle_fresh'] is False


def test_reference_available_only_after_decision_is_masked() -> None:
    ora = oracle(); ora['settlement_references']['m1']['available_wall_ns'] = NOW + 1
    _, out = build(ora=ora)
    assert out['markets'][0]['reference_valid'] is False
    assert out['markets'][0]['distance_to_reference_bp'] is None


def test_missing_derivative_fields_are_not_zero_or_unmasked() -> None:
    ext = all_external()
    value = ext['ETH']['derivative_contexts'][0]
    value.update(valid_mask=1, funding_rate=0.0, open_interest_native=0.0)
    _, out = build(ext=ext)
    derivative = out['markets'][0]['derivatives'][0]
    assert derivative['mark_price'] is not None
    assert derivative['index_price'] is None
    assert derivative['funding_rate'] is None
    assert derivative['open_interest_native'] is None


def test_missing_derivative_age_is_not_fresh_zero_age() -> None:
    ext = all_external(); ext['ETH']['derivative_contexts'][0].pop('age_ns')
    _, out = build(ext=ext)
    assert out['markets'][0]['derivatives'][0]['usable'] is False


def test_crossed_book_cannot_emit_price_or_imbalance_features() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp); write_books(directory)
        path = directory / 'yes1.json'; value = json.loads(path.read_text())
        value['best_bid'] = .9; value['best_ask'] = .1; path.write_text(json.dumps(value))
        out = FeatureEngine(policy()).build(external=all_external(), oracle=oracle(), selection=selection(),
                                          book_dir=directory, model_sha=SHA, now_ns=NOW)
        row = out['markets'][0]
        assert row['pm_book_valid'] is False
        assert row['pm_yes_mid'] is None and row['pm_yes_spread'] is None
        assert row['pm_yes_imbalance'] is None

def test_runtime_main_has_fail_closed_warmup_contract() -> None:
    source=(ROOT/'scripts/v7_multi_crypto_feature_engine.py').read_text()
    assert 'WARMING_OR_BLOCKED' in source
    assert 'runtime_blockers' in source
    assert 'except ValueError as error:' in source
    assert 'execution_authority": False' in source



def test_replay_clock_does_not_depend_on_host_wall_clock() -> None:
    original = feature_engine_module.time.time_ns
    def forbidden():
        raise AssertionError('wall clock used during deterministic replay')
    feature_engine_module.time.time_ns = forbidden
    try:
        _, out = build()
        assert out['timestamp_ns'] == NOW
    finally:
        feature_engine_module.time.time_ns = original


def test_live_clock_regression_is_explicitly_rejected() -> None:
    try:
        build(capture_clock=lambda: NOW - 1)
    except ValueError as exc:
        assert 'DECISION_CLOCK_INVALID_OR_REGRESSIVE' in str(exc)
    else:
        raise AssertionError('clock regression accepted')


def test_contract_state_blocks_active_market_until_ready() -> None:
    _, out = build(contract_state=contract_state('ACTIVE_BLOCKED'))
    row = out['markets'][0]
    assert row['contract_state_bound'] is True
    assert row['contract_state_state'] == 'ACTIVE_BLOCKED'
    assert 'CONTRACT_STATE_NOT_READY' in row['blockers']
    assert out['ready_for_calibration_markets'] == 0


def test_contract_state_ready_preserves_causal_shadow_readiness() -> None:
    _, out = build(contract_state=contract_state())
    row = out['markets'][0]
    assert row['contract_state_bound'] is True
    assert row['contract_state_state'] == 'ACTIVE_READY_SHADOW'
    assert row['contract_state_hash'] == 'e' * 64
    assert row['blockers'] == ['UNCALIBRATED_SHADOW']
    assert out['ready_for_calibration_markets'] == 1


def test_future_contract_state_is_not_available_at_decision() -> None:
    _, out = build(contract_state=contract_state(available_at_ns=NOW + 1))
    row = out['markets'][0]
    assert 'CONTRACT_STATE_NOT_READY' in row['blockers']
    assert 'FUTURE_OR_UNKNOWN_INPUT_AVAILABILITY' in row['blockers']


if __name__ == '__main__':
    tests = sorted((n, f) for n, f in globals().items() if n.startswith('test_') and callable(f))
    for _, fn in tests: fn()
    print(f'{len(tests)} function tests passed')
