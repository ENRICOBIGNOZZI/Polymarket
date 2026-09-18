"""Read-only, fill-linked allocation of aggregate native PAPER settlements.

The aggregate FINAL remains the only cash event. Rows emitted here are views,
never new ledger events. Old valid ledgers need no mutation or schema migration.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator


class ProjectionError(ValueError):
    """The available evidence cannot support a complete settlement view."""


def _money(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ProjectionError(field + ':missing')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProjectionError(field + ':invalid') from exc
    if not number.is_finite():
        raise ProjectionError(field + ':nonfinite')
    return number


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get('metadata')
    return value if isinstance(value, dict) else {}


def context_from_fill(row: dict[str, Any]) -> dict[str, str]:
    meta = _metadata(row)
    context = meta.get('crypto_context') or {}
    if not isinstance(context, dict):
        raise ProjectionError('crypto_context:invalid')
    asset = str(context.get('asset') or meta.get('asset') or row.get('asset') or '').upper()
    horizon = str(context.get('horizon') or meta.get('horizon') or row.get('horizon') or '').upper()
    # These two historical strategies have an explicit immutable BTC/M5 scope.
    # Generic maker/native engine names must never imply an asset by themselves.
    if not asset or not horizon:
        if meta.get('model_family') in {'crypto_informed_taker', 'lead_lag_taker_v1'}:
            if (asset and asset != 'BTC') or (horizon and horizon != 'M5'):
                raise ProjectionError('crypto_context:conflicts_with_frozen_strategy')
            asset, horizon = 'BTC', 'M5'
    if not asset or not horizon:
        raise ProjectionError('crypto_context:missing')
    return {'asset': asset, 'horizon': horizon}


def native_final(row: dict[str, Any]) -> bool:
    return row.get('event_type') == 'FINAL' and bool(_metadata(row).get('native_market_settlement_id'))


def allocate_final(final: dict[str, Any], fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate one aggregate FINAL and allocate all of its included fills.

    Signed settlement inventories allow a maker sale of inventory acquired by
    the taker. Component attribution is additive, not an independent risk book.
    """
    meta = _metadata(final)
    market = str(final.get('market_id') or '')
    sha = str(final.get('model_sha') or '')
    settlement = meta.get('native_market_settlement_id')
    if not market or settlement != 'native-settlement:' + market:
        raise ProjectionError('settlement_identity:invalid')
    if final.get('paper_only') is not True or final.get('authenticated_execution') is not False:
        raise ProjectionError('settlement_authority:invalid')
    if not fills:
        raise ProjectionError('settlement_fills:missing')
    ids = [str(row.get('fill_id') or '') for row in fills]
    included = meta.get('included_fill_ids')
    if not all(ids) or len(set(ids)) != len(ids):
        raise ProjectionError('fill_identity:duplicate_or_missing')
    if not isinstance(included, list) or not all(isinstance(x, str) and x for x in included) or len(set(included)) != len(included) or set(included) != set(ids):
        raise ProjectionError('settlement_fill_set:incomplete_or_conflicting')
    winner = str(meta.get('winning_token_id') or '')
    if not winner:
        raise ProjectionError('winning_token:missing')
    positions: dict[tuple[str, str], dict[str, Any]] = {}
    inventory: dict[str, Decimal] = defaultdict(Decimal)
    cash = Decimal(0)
    for fill in fills:
        if fill.get('model_sha') != sha or str(fill.get('market_id') or '') != market:
            raise ProjectionError('fill_scope:mismatch')
        if fill.get('paper_only') is not True or fill.get('authenticated_execution') is not False:
            raise ProjectionError('fill_authority:invalid')
        fm = _metadata(fill)
        receipt = fm.get('native_settlement_receipt') or {}
        if not isinstance(receipt, dict):
            raise ProjectionError('fill_receipt:invalid')
        if not isinstance(fill.get('order_id'), str) or not fill['order_id']:
            raise ProjectionError('fill_order:missing')
        if (receipt.get('owner') != 'V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE'
                or receipt.get('model_sha') != sha or receipt.get('single_owner') is not True):
            raise ProjectionError('fill_receipt:invalid')
        if meta.get('run_id') and fm.get('run_id') != meta.get('run_id'):
            raise ProjectionError('fill_run:mismatch')
        token, position = str(fill.get('token_id') or ''), str(fill.get('position_id') or '')
        if not token or not position:
            raise ProjectionError('fill_position:missing')
        qty, price, fee = (_money(fill.get(key), key) for key in ('filled_size', 'fill_price', 'fee'))
        if qty <= 0 or not 0 <= price <= 1 or fee < 0 or fill.get('side') not in {'BUY', 'SELL'}:
            raise ProjectionError('fill_economics:invalid')
        sign = Decimal(1) if fill['side'] == 'BUY' else Decimal(-1)
        inventory[token] += sign * qty
        if inventory[token] < Decimal('-0.000000001'):
            raise ProjectionError('inventory:naked_sell')
        delta_cash = -sign * qty * price - fee
        cash += delta_cash
        component = str(fm.get('component') or fm.get('model_family') or '')
        if not component:
            raise ProjectionError('component:missing')
        key = position, component
        context = context_from_fill(fill)
        group = positions.setdefault(key, {'position_id': position, 'token_id': token,
            'component': component, 'context': context, 'cash': Decimal(0),
            'quantity': Decimal(0), 'fills': [], 'orders': set()})
        if group['token_id'] != token or group['context'] != context:
            raise ProjectionError('position_scope:mismatch')
        group['cash'] += delta_cash
        group['quantity'] += sign * qty
        group['fills'].append(fill['fill_id'])
        group['orders'].add(fill.get('order_id'))
    payout = inventory.get(winner, Decimal(0))
    expected = cash + payout
    reported = _money(final.get('final_pnl'), 'final_pnl')
    tolerance = Decimal('0.00000001') * max(Decimal(1), abs(expected), abs(reported))
    if abs(expected - reported) > tolerance:
        raise ProjectionError('settlement_pnl:mismatch')
    if final.get('realized_cashflow') is not None:
        if abs(_money(final['realized_cashflow'], 'realized_cashflow') - payout) > tolerance:
            raise ProjectionError('settlement_payout:mismatch')
    explicit_positions = meta.get('included_position_ids')
    closed = {group['position_id'] for group in positions.values()}
    if explicit_positions is not None and (not isinstance(explicit_positions, list) or set(explicit_positions) != closed):
        raise ProjectionError('settlement_positions:mismatch')
    views = []
    for ordinal, group in enumerate(positions.values()):
        cashout = group['quantity'] if group['token_id'] == winner else Decimal(0)
        vm = dict(meta)
        vm.update({'component': group['component'], 'model_family': group['component'],
            'crypto_context': group['context'], 'projection_only': True,
            'canonical_final_record_id': final.get('record_id'),
            'included_fill_ids': list(group['fills']), 'included_order_ids': sorted(group['orders']),
            'won': group['token_id'] == winner, 'attribution_basis': 'SIGNED_FILL_CASHFLOW_PLUS_SETTLEMENT'})
        view = dict(final)
        view.update({'record_id': str(final.get('record_id') or settlement) + ':view:' + str(ordinal),
            'position_id': group['position_id'], 'token_id': group['token_id'],
            'final_pnl': float(group['cash'] + cashout), 'realized_cashflow': float(cashout), 'metadata': vm})
        views.append(view)
    return views


def iter_position_economics(rows: Iterable[dict[str, Any]], errors: list[str]) -> Iterator[dict[str, Any]]:
    """Expand FINALs as read-only position views; unknown cases remain explicit."""
    fills: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    closed: set[tuple[str, str]] = set()
    for row in rows:
        key = str(row.get('model_sha') or ''), str(row.get('market_id') or '')
        if row.get('event_type') == 'FILL' and _metadata(row).get('native_settlement_receipt'):
            if key in closed:
                errors.append('fill_after_final:' + key[1])
            fills[key].append(row)
            yield row
        elif native_final(row):
            if key in closed:
                errors.append('duplicate_native_final:' + key[1])
                continue
            closed.add(key)
            try:
                yield from allocate_final(row, fills.get(key, []))
            except ProjectionError as exc:
                errors.append(str(exc) + ':' + key[1])
                yield row
        else:
            yield row
