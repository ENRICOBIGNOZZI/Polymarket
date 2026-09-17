from __future__ import annotations
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from v7_lead_lag_replay import (ArrivalCut, ArrivalTape, BookCut, Clock, DepthDepletion, FeeTerms,
    LatencyScenario, ReplayError, ReplayIntent, ResolutionProof, decimal, digest,
    settlement_pnl, simulate_arrival)
T = 10_000_000_000
W = 1_800_000_000_000_000_000


def fixture(asset='ETH', horizon='M5', market='market-1', token='yes-1'):
    clock = Clock('synthetic-host', 'synthetic-boot', T, W)
    fee = FeeTerms(market, 'fee-A', D('.07'), 1, 'USDC', 'CASH', D('.00001'), 'HALF_EVEN',
                   T - 1_000_000, T + 200_000_000_000, 'SYNTHETIC_FEE_FIXTURE', True)
    book = BookCut(market, token, 'book-A', 'epoch-A', 1, 'a' * 64, 'fee-A', D('.01'), D('5'),
                   clock, T, ((D('.49'), D('20')),), ((D('.50'), D('20')),), True)
    cut = ArrivalCut(book, fee, T, W + 110_000_000_000, True, True, True, True, True,
                     False, False, True, 0)
    intent = ReplayIntent('trace-' + market, 'synthetic-cohort', 'b' * 64, 'signal-' + market,
                          'shock-1', asset, horizon, market, token, 'BUY', D('5'), D('.50'), clock,
                          T, T + 2_000_000_000, T + 5_000_000_000, 105_000_000_000,
                          120_000_000_000, 1_000_000_000, 1, 'epoch-A', 'a' * 64, 'fee-A',
                          D('.01'), 0, 'book-A')
    scenario = LatencyScenario('SYNTHETIC_5MS', 0, 5_000_000, 0, D(1))
    return intent, cut, scenario


def proof(market='market-1', token='yes-1', winner=True):
    return ResolutionProof(market, ((token, D(int(winner))), ('no-' + market, D(int(not winner)))),
                           'OFFICIAL_GAMMA_RESOLUTION', 'c' * 64, W + 120_000_000_000, 'RESOLVED')


class ReplayTest(unittest.TestCase):
    def test_full_fill_exact_cash_and_no_fake_ack(self):
        i, c, s = fixture()
        r = simulate_arrival(i, c, s, DepthDepletion())
        self.assertEqual((r.status, r.filled, r.cash_delta), ('FILLED', D(5), D('-2.58750')))
        self.assertIsNone(r.exchange_ack)
        self.assertIsNone(r.observed_exchange_execution_time)
        self.assertEqual(r.arrival_clock.monotonic_ns, T + 5_000_000)

    def test_partial_fak_cancels_residual(self):
        i, c, s = fixture()
        c = replace(c, book=replace(c.book, asks=((D('.50'), D(2)),)))
        r = simulate_arrival(i, c, s, DepthDepletion())
        self.assertEqual((r.status, r.filled, r.requested, r.cash_delta), ('PARTIAL', D(2), D(5), D('-1.03500')))

    def test_no_chase_and_better_price(self):
        i, c, s = fixture()
        worse = replace(c, book=replace(c.book, asks=((D('.51'), D(20)),)))
        self.assertEqual(simulate_arrival(i, worse, s, DepthDepletion()).reason, 'ARRIVAL_PRICE_WORSE')
        better = replace(c, book=replace(c.book, bids=((D('.47'), D(10)),), asks=((D('.48'), D(20)),)))
        self.assertEqual(simulate_arrival(i, better, s, DepthDepletion()).gross_cash, D('-2.40'))

    def test_multiple_depth_levels(self):
        i, c, s = fixture()
        c = replace(c, book=replace(c.book, bids=((D('.47'), D(10)),),
                  asks=((D('.48'), D(2)), (D('.49'), D(2)), (D('.50'), D(1)), (D('.51'), D(20)))))
        r = simulate_arrival(i, c, s, DepthDepletion())
        self.assertEqual((r.filled, r.gross_cash, len(r.levels)), (D(5), D('-2.44'), 3))

    def test_sell_uses_bid_not_mid(self):
        i, c, s = fixture()
        r = simulate_arrival(replace(i, side='SELL', limit_price=D('.49')), c, s, DepthDepletion())
        self.assertEqual(r.gross_cash, D('2.45'))
        self.assertLess(r.cash_delta, D('2.45'))

    def test_same_liquidity_not_reused_across_snapshots(self):
        i, c, s = fixture()
        pool = DepthDepletion()
        c = replace(c, book=replace(c.book, asks=((D('.50'), D(7)),)))
        self.assertEqual(simulate_arrival(i, c, s, pool).filled, D(5))
        c2 = replace(c, book=replace(c.book, snapshot_id='book-B'))
        self.assertEqual(simulate_arrival(replace(i, trace_id='trace-2'), c2, s, pool).filled, D(2))
        self.assertEqual(simulate_arrival(replace(i, trace_id='trace-3'), c2, s, pool).filled, D(0))

    def test_depth_stress(self):
        i, c, s = fixture()
        c = replace(c, book=replace(c.book, asks=((D('.50'), D(8)),)))
        self.assertEqual(simulate_arrival(i, c, replace(s, depth_fraction=D('.5')), DepthDepletion()).filled, D(4))

    def test_signal_and_authorization_expiry_inclusive(self):
        i, c, s = fixture()
        end = T + s.network_ns
        for field in ('expires_monotonic_ns', 'signal_expires_monotonic_ns'):
            self.assertEqual(simulate_arrival(replace(i, **{field: end}), c, s, DepthDepletion()).status, 'FILLED')
            self.assertEqual(simulate_arrival(replace(i, **{field: end - 1}), c, s, DepthDepletion()).status, 'REJECTED')

    def test_tte_boundaries(self):
        i, c, s = fixture()
        for tte in (105_000_000_000, 120_000_000_000):
            changed = replace(c, market_close_wall_ns=W + s.network_ns + tte)
            self.assertEqual(simulate_arrival(i, changed, s, DepthDepletion()).status, 'FILLED')
        changed = replace(c, market_close_wall_ns=W + s.network_ns + 104_999_999_999)
        self.assertEqual(simulate_arrival(i, changed, s, DepthDepletion()).reason, 'TTE_OUTSIDE_WINDOW')

    def test_matching_delay_is_in_arrival(self):
        i, c, s = fixture()
        i, c = replace(i, matching_delay_ns=1_000_000), replace(c, matching_delay_ns=1_000_000)
        self.assertEqual(simulate_arrival(i, c, s, DepthDepletion()).arrival_clock.monotonic_ns, T + 6_000_000)
        self.assertEqual(simulate_arrival(i, replace(c, matching_delay_ns=2_000_000), s, DepthDepletion()).reason, 'MATCHING_DELAY_CHANGED')

    def test_unchanged_book_fresh_transport(self):
        i, c, s = fixture()
        old = replace(c.book, clock=Clock('synthetic-host', 'synthetic-boot', T - 5_000_000_000, W - 5_000_000_000))
        self.assertEqual(simulate_arrival(i, replace(c, book=old), s, DepthDepletion()).status, 'FILLED')

    def test_heartbeat_does_not_redefine_snapshot(self):
        i, c, s = fixture()
        heartbeat = replace(c, available_monotonic_ns=T+1,
                            book=replace(c.book, transport_monotonic_ns=T+1))
        self.assertEqual(ArrivalTape([c, heartbeat]).at(i, s.arrival(i)), heartbeat)

    def test_clock_domains_never_subtracted(self):
        i, c, s = fixture()
        for name, value in (('host_id', 'other'), ('boot_id', 'other')):
            changed = replace(c, book=replace(c.book, clock=replace(c.book.clock, **{name: value})))
            self.assertEqual(simulate_arrival(i, changed, s, DepthDepletion()).reason, 'CLOCK_DOMAIN_MISMATCH')

    def test_future_or_stale_book_unknown_not_zero(self):
        i, c, s = fixture()
        for changed in (None, replace(c, available_monotonic_ns=T + 10_000_000)):
            r = simulate_arrival(i, changed, s, DepthDepletion())
            self.assertEqual(r.status, 'UNKNOWN')
            self.assertIsNone(settlement_pnl(i, r, proof(), as_of_wall_ns=W + 200_000_000_000))
        r = simulate_arrival(replace(i, max_transport_age_ns=1), c, s, DepthDepletion())
        self.assertEqual(r.reason, 'PM_BOOK_TOO_OLD')

    def test_reference_call_graph_forbids_http_files_and_sleep(self):
        i, c, s = fixture()
        with patch('builtins.open', side_effect=AssertionError('FILE I/O')), \
             patch('socket.socket', side_effect=AssertionError('NETWORK')), \
             patch('time.sleep', side_effect=AssertionError('SLEEP')):
            self.assertEqual(simulate_arrival(i, c, s, DepthDepletion()).status, 'FILLED')

    def test_fee_unknown_never_zero(self):
        i, c, s = fixture()
        for fee in (replace(c.fee, verified=False), replace(c.fee, expires_ns=T + 1)):
            r = simulate_arrival(i, replace(c, fee=fee), s, DepthDepletion())
            self.assertEqual((r.status, r.reason), ('UNKNOWN', 'FEES_UNKNOWN'))

    def test_share_fee_not_debited_twice(self):
        i, c, s = fixture()
        r = simulate_arrival(i, replace(c, fee=replace(c.fee, incidence='BUY_SHARES')), s, DepthDepletion())
        self.assertEqual(r.cash_delta, D('-2.50'))
        self.assertEqual(r.net_shares, D('4.825'))
        self.assertEqual(settlement_pnl(i, r, proof(), as_of_wall_ns=W + 200_000_000_000), D('2.325'))

    def test_share_fee_has_explicit_fixed_precision(self):
        _, c, _ = fixture()
        fee = replace(c.fee, incidence='BUY_SHARES', share_quantum=D('.000001'))
        self.assertEqual(fee.shares_charge(D('.00001'), D('.3')), D('.000033'))

    def test_cash_fee_settlement_exact_identity(self):
        i, c, s = fixture()
        r = simulate_arrival(i, c, s, DepthDepletion())
        self.assertEqual(settlement_pnl(i, r, proof(), as_of_wall_ns=W + 200_000_000_000), D('2.41250'))
        self.assertEqual(settlement_pnl(i, r, proof(winner=False), as_of_wall_ns=W + 200_000_000_000), D('-2.58750'))

    def test_unresolved_and_future_resolution_stay_missing(self):
        i, c, s = fixture()
        r = simulate_arrival(i, c, s, DepthDepletion())
        self.assertIsNone(settlement_pnl(i, r, None, as_of_wall_ns=W + 200_000_000_000))
        self.assertIsNone(settlement_pnl(i, r, proof(), as_of_wall_ns=W + 1))
        self.assertIsNone(settlement_pnl(i, r, replace(proof(), status='UNRESOLVED', token_payouts=()), as_of_wall_ns=W + 200_000_000_000))

    def test_closed_without_explicit_resolution_is_unresolved(self):
        raw = {'id': 'm', 'closed': True, 'outcomePrices': '["1", "0"]', 'clobTokenIds': '["up", "down"]'}
        p = ResolutionProof.from_gamma(raw, expected_market='m', expected_tokens=('up', 'down'), available_wall_ns=W)
        self.assertEqual(p.status, 'UNRESOLVED')
        raw['umaResolutionStatus'] = 'resolved'
        raw['clobTokenIds'], raw['outcomePrices'] = '["down", "up"]', '["0", "1"]'
        p = ResolutionProof.from_gamma(raw, expected_market='m', expected_tokens=('up', 'down'), available_wall_ns=W)
        self.assertEqual(dict(p.token_payouts)['up'], D(1))
        raw['outcomePrices'] = '["0.005", "0.995"]'
        self.assertEqual(ResolutionProof.from_gamma(raw, expected_market='m', expected_tokens=('up', 'down'), available_wall_ns=W).status, 'UNRESOLVED')

    def test_resolution_proxy_rejected(self):
        with self.assertRaisesRegex(ReplayError, 'SOURCE_UNVERIFIED'):
            replace(proof(), source='LOCAL_SPOT_PROXY')

    def test_resolution_cannot_precede_entry(self):
        i, c, s = fixture()
        r = simulate_arrival(i, c, s, DepthDepletion())
        with self.assertRaisesRegex(ReplayError, 'PRECEDES_ENTRY'):
            settlement_pnl(i, r, replace(proof(), available_wall_ns=W), as_of_wall_ns=W+200_000_000_000)

    def test_causal_tape_does_not_use_future(self):
        i, c, s = fixture()
        changed = replace(c, available_monotonic_ns=T + 10_000_000,
                          book=replace(c.book, snapshot_id='book-B', clock=c.book.clock.after(10_000_000),
                                       transport_monotonic_ns=T + 10_000_000, asks=((D('.60'), D(100)),)))
        tape = ArrivalTape([changed, c])
        self.assertEqual(tape.at(i, s.arrival(i)), c)
        self.assertEqual(tape.at(i, i.clock.after(10_000_000)), changed)

    def test_tape_never_falls_back_from_invalid_latest(self):
        i, c, s = fixture()
        bad = replace(c, available_monotonic_ns=T + 1, book=replace(c.book, snapshot_id='bad', valid=False))
        self.assertFalse(ArrivalTape([c, bad]).at(i, s.arrival(i)).book.valid)

    def test_ambiguous_tape_and_redefined_snapshot_rejected(self):
        i, c, s = fixture()
        with self.assertRaises(ReplayError):
            ArrivalTape([c, replace(c, book=replace(c.book, asks=((D('.51'), D(20)),)))])
        with self.assertRaisesRegex(ReplayError, 'AMBIGUOUS_CUT_ORDER'):
            ArrivalTape([c, replace(c, kill=True)])

    def test_invalid_numbers_and_book_fail(self):
        for value in (True, 'NaN', 'Infinity', float('nan'), None):
            with self.subTest(value=value), self.assertRaises(ReplayError):
                decimal(value)
        _, c, _ = fixture()
        for levels in (((D('.501'), D(1)),), ((D('.50'), D(-1)),), ((D('.50'), D(1)), (D('.50'), D(1)))):
            with self.assertRaises(ReplayError):
                replace(c.book, asks=levels)

    def test_unknown_scope_has_no_btc_fallback(self):
        i, _, _ = fixture()
        with self.assertRaisesRegex(ReplayError, 'UNKNOWN_ASSET'):
            replace(i, asset='MISSING')
        with self.assertRaisesRegex(ReplayError, 'UNSUPPORTED_HORIZON'):
            replace(i, horizon='M1')

    def test_network_zero_is_not_assumed(self):
        _, _, s = fixture()
        with self.assertRaisesRegex(ReplayError, 'POSITIVE_NETWORK'):
            replace(s, network_ns=0)

    def test_attempt_identity_unchanged_after_restart(self):
        i, _, _ = fixture()
        self.assertEqual(i.attempt_id, replace(i, clock=replace(i.clock, boot_id='restart')).attempt_id)
        self.assertNotEqual(i.attempt_id, replace(i, signal_id='other').attempt_id)


for _asset in ('BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB'):
    for _horizon in ('M5', 'M15'):
        def _asset_test(self, asset=_asset, horizon=_horizon):
            i, c, s = fixture(asset, horizon)
            self.assertEqual(simulate_arrival(i, c, s, DepthDepletion()).status, 'FILLED')
        setattr(ReplayTest, 'test_shared_' + _asset + '_' + _horizon, _asset_test)

for _field, _value, _reason in (
    ('kill', True, 'KILL'), ('cutover_drain', True, 'CUTOVER_DRAIN'),
    ('accounting_ready', False, 'ACCOUNTING_MISMATCH'), ('rules_verified', False, 'RULES_UNVERIFIED'),
    ('reference_ready', False, 'MISSING_REFERENCE'), ('oracle_ready', False, 'ORACLE_STALE'),
    ('required_feeds_ready', False, 'EXTERNAL_FEED_GAP'), ('accepting_orders', False, 'MARKET_CLOSED')):
    def _gate_test(self, field=_field, value=_value, reason=_reason):
        i, c, s = fixture()
        self.assertEqual(simulate_arrival(i, replace(c, **{field: value}), s, DepthDepletion()).reason, reason)
    setattr(ReplayTest, 'test_gate_' + _field, _gate_test)

for _field, _value, _reason in (
    ('token_id', 'other', 'TOKEN_MAPPING_MISMATCH'), ('generation', 2, 'ROLLOVER_GENERATION_MISMATCH'),
    ('epoch', 'restart', 'PM_BOOK_UNSYNCED'), ('rules_hash', 'other', 'RULES_CHANGED'),
    ('tick', D('.005'), 'TICK_CHANGED'), ('fee_version', 'new', 'FEE_VERSION_CHANGED')):
    def _version_test(self, field=_field, value=_value, reason=_reason):
        i, c, s = fixture()
        self.assertEqual(simulate_arrival(i, replace(c, book=replace(c.book, **{field: value})), s, DepthDepletion()).reason, reason)
    setattr(ReplayTest, 'test_version_' + _field, _version_test)

if __name__ == '__main__':
    unittest.main()
