#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from v7_execution_ledger import LedgerEvent
from v7_ledger_history_audit import amount, audit_events, audit_paths, file_hash


def fixture():
    common = dict(strategy='TEST', model_sha='1'*40, order_id='order', market_id='market',
                  token_id='token', side='BUY', book_snapshot_id='book', exchange_ts_ms=997,
                  receive_ts_ms=998, decision_ts_ms=999, recorded_ts_ms=1000,
                  metadata={'coordinator_receipt': {'owner': 'V7_GLOBAL_PORTFOLIO_COORDINATOR'},
                            'model_family': 'test', 'hold_to_settlement': True})
    submit = LedgerEvent(event_type='ORDER_SUBMITTED', record_id='submit', intended_action='TAKE',
                         intended_size=2, **common)
    fill = LedgerEvent(event_type='FILL', record_id='fill', fill_id='fill-id', filled_size=2,
                       fill_price=.4, fee=.01, fee_source='TEST', **common)
    final = LedgerEvent(event_type='FINAL', record_id='final', final_pnl=1.19,
                        realized_cashflow=2, fee=0, **common)
    return [submit, fill, final]


class LedgerHistoryAuditTests(unittest.TestCase):
    def test_consistent_cash_identity_does_not_claim_external_proof(self):
        result = audit_events(fixture())
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['orders'][0]['accounting_identity'], 'CONSISTENT')
        self.assertIsNone(result['independently_verified_pnl'])
        self.assertFalse(result['whole_account_cash_reconciled'])

    def test_duplicate_record_is_detected_not_double_counted(self):
        rows = fixture()
        result = audit_events(rows + [rows[-1]])
        self.assertIn('DUPLICATE_RECORD', result['issue_counts'])
        self.assertEqual(result['reported_total_pnl'], '1.19')

    def test_duplicate_fill_across_record_ids_is_detected(self):
        rows = fixture()
        result = audit_events(rows + [replace(rows[1], record_id='other')])
        self.assertIn('DUPLICATE_FILL', result['issue_counts'])

    def test_missing_terminal_stays_pending(self):
        result = audit_events(fixture()[:2])
        self.assertEqual(result['orders'][0]['status'], 'OPEN_OR_PENDING')
        self.assertIsNone(result['orders'][0]['reported_pnl'])

    def test_inconsistent_pnl_is_not_accepted(self):
        rows = fixture(); rows[-1] = replace(rows[-1], final_pnl=9)
        self.assertIn('PNL_IDENTITY_MISMATCH', audit_events(rows)['issue_counts'])

    def test_missing_receipt_is_reported(self):
        rows = fixture(); rows[-1] = replace(rows[-1], metadata={})
        self.assertIn('COORDINATOR_RECEIPT_MISSING', audit_events(rows)['issue_counts'])

    def test_unknown_fee_incidence_is_not_guessed(self):
        rows = fixture(); rows[1] = replace(rows[1], metadata={**rows[1].metadata, 'fee_incidence': 'SHARES'})
        result = audit_events(rows)
        self.assertEqual(result['orders'][0]['accounting_identity'], 'UNSUPPORTED_FEE_INCIDENCE_OR_SIDE')

    def test_source_file_is_unchanged_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.jsonl'
            path.write_text(''.join(json.dumps(e.to_dict())+'\n' for e in fixture()))
            before = file_hash(path)
            result = audit_paths([path])
            self.assertEqual(before, file_hash(path))
            self.assertEqual(result['inputs'][0]['sha256'], before)
            self.assertFalse(result['ledger_mutated'])

    def test_duplicate_input_paths_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_INPUT_PATH'):
            audit_paths([Path('same'), Path('same')])

    def test_missing_amount_is_not_zero(self):
        for value in [None, True, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                amount(value)


if __name__ == '__main__':
    unittest.main()
