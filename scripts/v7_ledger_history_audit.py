#!/usr/bin/env python3
"""Read-only ledger inspection; source records are never changed."""
from __future__ import annotations
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from v7_execution_ledger import EconomicJournalEntry, LedgerEvent, iter_records

ZERO = Decimal('0')
TOLERANCE = Decimal('0.00000001')


def amount(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError('Missing monetary input')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Nonfinite monetary input')
    return result


def lane(event: LedgerEvent) -> str:
    return str(event.metadata.get('model_family') or
               event.metadata.get('component') or event.strategy)

def audit_events(events: Iterable[LedgerEvent]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    issues: list[dict[str, str]] = []
    seen: dict[str, set[str]] = defaultdict(set)
    grouped: dict[str, list[LedgerEvent]] = defaultdict(list)
    synthetic_patterns = 0
    lineage: set[str] = set()
    def issue(code: str, e: LedgerEvent) -> None:
        issues.append({'code': code, 'record_id': e.record_id})
    for e in events:
        e.validate()
        counts[e.event_type] += 1
        lineage.add(e.model_sha)
        identities = [('record', e.record_id)]
        if e.event_type == 'FILL':
            identities.append(('fill', str(e.fill_id)))
        if e.event_type == 'FINAL':
            identities.append(('final', str(e.metadata.get('terminal_id') or
                              e.position_id or e.order_id or e.record_id)))
        duplicate = False
        for kind, identity in identities:
            if identity in seen[kind]:
                issue('DUPLICATE_' + kind.upper(), e)
                duplicate = True
            seen[kind].add(identity)
        if duplicate:
            continue
        if e.event_type in ('ORDER_SUBMITTED', 'FILL', 'FINAL'):
            receipt = e.metadata.get('coordinator_receipt')
            if not isinstance(receipt, dict) or not receipt:
                issue('COORDINATOR_RECEIPT_MISSING', e)
        if e.event_type == 'FILL' and e.decision_ts_ms is not None:
            synthetic_patterns += int(e.recorded_ts_ms == e.decision_ts_ms + 1)
        if e.order_id:
            grouped[e.order_id].append(e)
        elif e.event_type in ('FILL', 'FINAL'):
            issue('MONETARY_ORDER_LINK_MISSING', e)
    by_lane: dict[str, Decimal] = defaultdict(lambda: ZERO)
    orders: list[dict[str, Any]] = []
    for order_id, history in sorted(grouped.items()):
        submissions = [e for e in history if e.event_type == 'ORDER_SUBMITTED']
        fills = [e for e in history if e.event_type == 'FILL']
        finals = [e for e in history if e.event_type == 'FINAL']
        if len(submissions) > 1:
            issue('DUPLICATE_ORDER_SUBMISSION', submissions[-1])
        if fills and not submissions:
            issue('FILL_WITHOUT_SUBMISSION_IN_SCOPE', fills[0])
        if finals and not fills:
            issue('FINAL_WITHOUT_FILL_IN_SCOPE', finals[-1])
        if len(finals) > 1:
            issue('MULTIPLE_FINALS_FOR_ORDER', finals[-1])
        if not fills and not finals:
            continue
        row = {'order_id': order_id, 'lane': lane(history[0]),
               'market_id': history[0].market_id, 'fill_count': len(fills),
               'final_count': len(finals), 'accounting_identity': 'UNVERIFIED'}
        if not finals:
            row.update(status='OPEN_OR_PENDING', reported_pnl=None,
                       resolution_evidence='UNRESOLVED_OR_NOT_SUPPLIED')
        else:
            final = finals[-1]
            pnl = amount(final.final_pnl)
            by_lane[lane(final)] += pnl
            row.update(status='REPORTED_FINAL_NOT_INDEPENDENTLY_VERIFIED',
                       reported_pnl=str(pnl), token_id=final.token_id,
                       final_timestamp_ms=final.recorded_ts_ms,
                       resolution_evidence='RAW_RESOLUTION_PROOF_NOT_VALIDATED')
            if final.metadata.get('hold_to_settlement') is True and fills and len(finals) == 1:
                explicit_cash = all(e.side == 'BUY' and e.metadata.get('fee_incidence',
                                    'LEGACY_CASH') in ('CASH', 'LEGACY_CASH') for e in fills)
                if explicit_cash:
                    cost = sum((amount(e.fill_price) * amount(e.filled_size) +
                                amount(e.fee) for e in fills), ZERO)
                    calculated = amount(final.realized_cashflow) - cost - amount(final.fee)
                    delta = pnl - calculated
                    row.update(recomputed_pnl=str(calculated), identity_delta=str(delta),
                               fee_convention='AS_RECORDED_NOT_EXCHANGE_VERIFIED')
                    consistent = abs(delta) <= TOLERANCE
                    row['accounting_identity'] = 'CONSISTENT' if consistent else 'MISMATCH'
                    if not consistent:
                        issue('PNL_IDENTITY_MISMATCH', final)
                else:
                    row['accounting_identity'] = 'UNSUPPORTED_FEE_INCIDENCE_OR_SIDE'
        orders.append(row)
    return {
        'schema': 'polymarket_v7_ledger_history_audit_v1',
        'input_scope': 'EXPLICIT_LEDGER_FILES_ONLY',
        'state': 'INTEGRITY_FINDINGS' if issues else 'STRUCTURALLY_CONSISTENT_PROOF_PENDING',
        'event_counts': dict(counts), 'lineage_shas': sorted(lineage),
        'issues': issues, 'issue_counts': dict(Counter(x['code'] for x in issues)),
        'reported_pnl_by_lane': {k: str(v) for k, v in sorted(by_lane.items())},
        'reported_total_pnl': str(sum(by_lane.values(), ZERO)),
        'independently_verified_pnl': None, 'whole_account_cash_reconciled': False,
        'decision_plus_one_ms_fill_pattern_count': synthetic_patterns,
        'pattern_is_not_exchange_latency': True, 'orders': orders,
        'ledger_mutated': False, 'paper_only': True,
        'authenticated_execution': False, 'real_order_submission': False,
    }


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def audit_paths(paths: list[Path]) -> dict[str, Any]:
    inputs: list[dict[str, Any]] = []
    journals = 0
    resolved = [p.resolve() for p in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError('DUPLICATE_INPUT_PATH')
    def events() -> Iterable[LedgerEvent]:
        nonlocal journals
        for path in resolved:
            before = file_hash(path)
            for record in iter_records(path):
                if isinstance(record, EconomicJournalEntry):
                    journals += 1
                else:
                    yield record
            if before != file_hash(path):
                raise ValueError('INPUT_CHANGED_DURING_AUDIT_USE_SNAPSHOT')
            inputs.append({'path': str(path), 'sha256': before,
                           'size_bytes': path.stat().st_size})
    result = audit_events(events())
    result.update(inputs=inputs, validated_journal_entries=journals)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, action='append', required=True)
    args = parser.parse_args()
    try:
        result = audit_paths(args.ledger)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({'state': 'AUDIT_BLOCKED', 'reason': str(exc), 'ledger_mutated': False}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 2 if result['issues'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
