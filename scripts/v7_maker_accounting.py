"""Canonical Maker projection/settlement inside the single crypto account owner.

No writer, process, order submission or second account lives here. The existing
router reconstructs positions from the canonical ledger/spool and sends verified
terminal events to the existing single ledger writer. Old numeric order IDs are
qualified by market; new executor IDs are already globally qualified.
"""
from __future__ import annotations
import hashlib
import json
import math
from typing import Any
from v7_execution_ledger import LedgerEvent


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError('maker_account:' + name)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('maker_account:' + name)
    return result


def canonical_maker(event: LedgerEvent) -> bool:
    m = event.metadata or {}
    receipt = m.get('coordinator_receipt') or {}
    return (event.strategy.upper() == 'MICRO_MAKER_PRO'
        and event.paper_only is True and event.authenticated_execution is False
        and m.get('paper_exploration') is True
        and m.get('economic_authority') == 'PAPER_EXPLORATION'
        and m.get('counterfactual') is not True
        and m.get('excluded_from_portfolio_equity') is not True
        and receipt.get('owner') == 'V7_GLOBAL_PORTFOLIO_COORDINATOR'
        and receipt.get('action') == 'MAKE' and receipt.get('paper_only') is True
        and receipt.get('paper_exploration_authorized') is True
        and receipt.get('authenticated_execution') is False
        and receipt.get('real_order_submission') is False)


def project_maker(events: list[LedgerEvent], cached: dict[str, Any]) -> dict[str, Any]:
    orders, fills, finals, terminal_orders = {}, {}, {}, set()
    issues = []
    for e in events:
        if not canonical_maker(e):
            continue
        if e.side != 'BUY':
            issues.append('unsupported_maker_side:' + e.record_id)
            continue
        if e.event_type == 'ORDER_SUBMITTED':
            key = (str(e.market_id), str(e.order_id))
            if key in orders and orders[key].record_id != e.record_id:
                issues.append('duplicate_maker_order:' + str(key))
            orders[key] = e
        elif e.event_type in {'FILL', 'FINAL'}:
            key = (str(e.market_id), str(e.fill_id))
            bucket = fills if e.event_type == 'FILL' else finals
            if not e.fill_id or key in bucket and bucket[key].record_id != e.record_id:
                issues.append('duplicate_or_missing_maker_fill:' + str(key))
            bucket[key] = e
        elif e.event_type == 'ORDER_STATE' and e.order_state in {'CANCELLED','EXPIRED','REJECTED','NONFILL'}:
            terminal_orders.add((str(e.market_id), str(e.order_id)))
    positions = {}; debit = payout = realized = marked = open_debit = 0.0
    filled_orders = set(); total_shares_by_order = {}
    for key, f in fills.items():
        order_key = (str(f.market_id), str(f.order_id)); order = orders.get(order_key)
        filled_orders.add(order_key)
        if order is None or order.token_id != f.token_id or order.side != f.side:
            issues.append('maker_fill_order_mismatch:' + str(key)); continue
        quantity = _number(f.filled_size, 'quantity'); price = _number(f.fill_price, 'price')
        fee = _number(f.fee, 'fee')
        if quantity <= 0 or not 0 < price < 1 or fee < 0:
            issues.append('maker_fill_economics:' + str(key)); continue
        total_shares_by_order[order_key] = total_shares_by_order.get(order_key, 0.) + quantity
        cost = price * quantity; debit += cost + fee
        identity = f.position_id or 'maker-position-legacy-' + hashlib.sha256(
            (f.model_sha + '|' + str(f.market_id) + '|' + str(f.fill_id)).encode()).hexdigest()
        final = finals.get(key)
        if final is not None:
            paid = _number(final.realized_cashflow, 'payout'); pnl = _number(final.final_pnl, 'pnl')
            won = (final.metadata or {}).get('won')
            if (final.position_id != identity or final.token_id != f.token_id
                    or final.market_id != f.market_id or final.side != f.side or not isinstance(won, bool)
                    or abs(paid - (quantity if won else 0.)) > 1e-7
                    or abs(pnl - (paid-cost-fee)) > 1e-7):
                issues.append('maker_final_cash_identity:' + str(key)); continue
            payout += paid; realized += pnl; continue
        prior = cached.get(identity) or {}
        executable = max(0., min(quantity, _number(prior.get('executable_value', 0.), 'mark')))
        open_debit += cost + fee; marked += executable
        positions[identity] = {
            'position_id': identity, 'fill_id': f.fill_id, 'order_id': f.order_id,
            'market_id': f.market_id, 'event_id': f.event_id, 'token_id': f.token_id,
            'model_sha': f.model_sha, 'model_version': f.model_version,
            'strategy': f.strategy, 'canonical_maker': True,
            'shares': quantity, 'entry_price': price, 'entry_cost': cost,
            'entry_fee': fee, 'entry_debit': cost+fee, 'executable_value': executable,
            'opened_ms': f.receive_ts_ms or f.recorded_ts_ms, 'settled': False,
            'settlement_attempt_ms': prior.get('settlement_attempt_ms', 0),
            'fill_record_id': f.record_id, 'fill_metadata': f.metadata,
            'coordinator_receipt': (f.metadata or {}).get('coordinator_receipt'),
        }
    for key, amount in total_shares_by_order.items():
        if amount > _number(orders[key].intended_size, 'order_size') + 1e-6:
            issues.append('maker_order_overfilled:' + str(key))
    for key in finals:
        if key not in fills:issues.append('maker_final_without_fill:' + str(key))
    return {'orders_submitted': len(orders), 'fills': len(fills),
        'terminal_nonfills': len(terminal_orders - filled_orders),
        'pending_orders': len(set(orders) - terminal_orders - filled_orders),
        'terminal_positions': len(finals), 'positions': positions,
        'entry_debit': debit, 'settlement_payout': payout, 'realized_pnl': realized,
        'marked_open_value': marked, 'open_entry_debit': open_debit,
        'traded_markets': sorted({str(f.market_id) for f in fills.values()}),
        'probe_fills': sum((f.metadata or {}).get('paper_bootstrap_probe') is True for f in fills.values()),
        'issues': sorted(set(issues)), 'accounting_source': 'CANONICAL_MAKER_FILL_FINAL_PROJECTION'}


def settlement_event(position: dict[str, Any], raw: dict[str, Any], received_ms: int) -> LedgerEvent | None:
    if (raw.get('closed') is not True or str(raw.get('id')) != str(position['market_id'])
            or received_ms <= int(position['opened_ms'])):
        return None
    def array(value):
        return json.loads(value) if isinstance(value, str) else value
    tokens = array(raw.get('clobTokenIds')); prices = array(raw.get('outcomePrices'))
    if not isinstance(tokens, list) or not isinstance(prices, list) or len(tokens) != 2 or len(prices) != 2:
        return None
    tokens = [str(x) for x in tokens]; prices = [_number(x, 'outcome_price') for x in prices]
    if len(set(tokens)) != 2 or str(position['token_id']) not in tokens or sorted(prices) != [0.,1.]:
        return None
    winner = tokens[prices.index(1.)]; won = winner == str(position['token_id'])
    payout = float(position['shares']) if won else 0.
    pnl = payout - float(position['entry_cost']) - float(position['entry_fee'])
    record_id = 'maker-final-' + hashlib.sha256((position['model_sha'] + '|' + position['position_id']).encode()).hexdigest()
    metadata = dict(position['fill_metadata'])
    metadata.update(paper_exploration=True, economic_authority='PAPER_EXPLORATION',
        counterfactual=False,excluded_from_portfolio_equity=False,research_evidence_only=False,
        realized=True,unwind_accounted=True,cost_vector_complete=True,cash_identity_verified=True,
        won=won,winning_token_id=winner,hold_to_settlement=True,
        settlement_provider='POLYMARKET_GAMMA_PUBLIC',settlement_closed=True,
        settlement_token_ids=tokens,settlement_outcome_prices=prices,
        settlement_observed_ms=received_ms,settlement_payout=payout,
        canonical_maker_fill_record_id=position['fill_record_id'],
        entry_debit=position['entry_cost']+position['entry_fee'],
        terminal_id=record_id,pnl_decomposition={'trading_pnl':pnl,'spread_capture':0.,
        'adverse_markout':0.,'inventory_pnl':0.,'maker_rebates':0.,'liquidity_rewards':0.,
        'own_reward_share_verified':False})
    return LedgerEvent(event_type='FINAL',strategy='MICRO_MAKER_PRO',model_sha=position['model_sha'],
        model_version=position.get('model_version'),record_id=record_id,recorded_ts_ms=received_ms,
        order_id=position['order_id'],fill_id=position['fill_id'],position_id=position['position_id'],
        market_id=position['market_id'],event_id=position['event_id'],token_id=position['token_id'],
        side='BUY',intended_action='MAKE',final_pnl=pnl,realized_cashflow=payout,
        fee=0.,slippage=0.,unwind_loss=0.,capital_cost=0.,latency_cost=0.,
        capital_duration_ms=received_ms-int(position['opened_ms']),metadata=metadata)


def authorized_maker_flat_proof(root, model_sha: str) -> dict[str, Any]:
    """Strict post-stop ledger proof; absence of an old Maker state is irrelevant."""
    from pathlib import Path
    from v7_execution_ledger import iter_records
    root = Path(root)
    def read(name):
        p = root / name
        if p.is_symlink():raise ValueError('authorized_maker_cutover:symlink')
        return json.loads(p.read_text())
    runtime = read('control/runtime_status.json')
    executor_path = root / 'micro_maker/authorized_make_executor_status.json'
    executor = read('micro_maker/authorized_make_executor_status.json')
    if (runtime.get('model_sha') != model_sha or runtime.get('state') not in {'stopping','stopped'}
            or runtime.get('economic_new_risk_ready') is not False
            or runtime.get('authorized_alpha_actions') != []):
        raise ValueError('authorized_maker_cutover:runtime_not_stopped_safe')
    for value in (runtime, executor):
        if (value.get('model_sha') != model_sha or value.get('paper_only') is not True
                or value.get('authenticated_execution') is not False
                or value.get('real_order_submission') is not False):
            raise ValueError('authorized_maker_cutover:identity_or_authority')
    if executor.get('active_orders') != 0:
        raise ValueError('authorized_maker_cutover:live_orders')
    spool = root / 'ledger/spool'
    if spool.exists() and any(spool.glob('*.json')):
        raise ValueError('authorized_maker_cutover:undrained_spool')
    ledger = root / 'ledger/execution.jsonl'
    if not ledger.is_file() or ledger.is_symlink():
        raise ValueError('authorized_maker_cutover:ledger_missing')
    records = list(iter_records(ledger))
    # Revalidate the public event representation instead of comparing Python
    # class identities. File-based module loaders can instantiate an equivalent
    # LedgerEvent class; silently skipping it would manufacture a flat account.
    events = []
    for record in records:
        if getattr(record, 'model_sha', None) == model_sha and hasattr(record, 'event_type'):
            if not callable(getattr(record, 'to_dict', None)):
                raise ValueError('authorized_maker_cutover:unrecognized_event_representation')
            events.append(LedgerEvent.from_dict(record.to_dict()))
    for e in events:
        if (e.strategy.upper() in {'MICRO_MAKER_PRO','MICRO_MAKER','PROFESSIONAL_MAKER'}
                and e.event_type in {'ORDER_SUBMITTED','FILL','FINAL','ORDER_STATE','INVENTORY_LIQUIDATION'}
                and not canonical_maker(e)):
            raise ValueError('authorized_maker_cutover:unrecognized_maker_authority')
    projection = project_maker(events, {})
    if projection['issues'] or projection['positions'] or projection['pending_orders']:
        raise ValueError('authorized_maker_cutover:unreconciled_inventory_or_orders')
    return {'checked_model_sha':model_sha,'runtime_state':runtime['state'],
            'ledger_sha256':hashlib.sha256(ledger.read_bytes()).hexdigest(),
            'ledger_bytes':ledger.stat().st_size,'ledger_records':len(records),
            'executor_status_sha256':hashlib.sha256(executor_path.read_bytes()).hexdigest(),
            'orders_submitted':projection['orders_submitted'],'fills':projection['fills'],
            'terminal_positions':projection['terminal_positions'],'pending_orders':0,
            'open_positions':0,'active_orders':0,'issues':[],
            'historical_realized_pnl':projection['realized_pnl'],
            'historical_entry_debit':projection['entry_debit'],
            'historical_settlement_payout':projection['settlement_payout']}
