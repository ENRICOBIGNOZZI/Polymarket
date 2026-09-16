#!/usr/bin/env python3
"""Durable reservation projection for the EXISTING V7 global coordinator.

No service, wallet, allocator, writer or execution authority is created here.
The owner supplies its reconciled account checkpoint and its existing canonical
writer's synchronous append callback. A successful append is a durability
barrier, not a RAM queue acknowledgement. Frozen BTC does not call this module.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
import threading
from typing import Any, Callable, Iterable, Mapping
from v7_execution_ledger import LedgerEvent
from v7_lead_lag_replay import (ASSETS, HORIZONS, ZERO, ReplayError, ResolutionProof,
                               decimal, digest, positive_int, primitive)

OWNER = 'V7_GLOBAL_PORTFOLIO_COORDINATOR'
SCHEMA = 'polymarket_v7_coordinator_reservation_projection_v1'
Append = Callable[[LedgerEvent], None]


@dataclass(frozen=True)
class ReservationRequest:
    experiment_id: str
    protocol_hash: str
    market_id: str
    token_id: str
    signal_id: str
    parent_shock_id: str
    asset: str
    horizon: str
    strategy: str
    currency: str
    maximum_debit: Decimal
    quantity: Decimal
    limit_price: Decimal
    expires_wall_ms: int
    coordinator_replay_key: str

    def __post_init__(self) -> None:
        for field in ('experiment_id', 'protocol_hash', 'market_id', 'token_id', 'signal_id',
                      'parent_shock_id', 'strategy', 'currency', 'coordinator_replay_key'):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ReplayError('RESERVATION_IDENTITY_MISSING:' + field)
        if len(self.protocol_hash) != 64 or any(c not in '0123456789abcdef' for c in self.protocol_hash):
            raise ReplayError('RESERVATION_PROTOCOL_INVALID')
        if self.asset not in ASSETS or self.horizon not in HORIZONS:
            raise ReplayError('RESERVATION_SCOPE_INVALID')
        for value in (self.maximum_debit, self.quantity, self.limit_price):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ReplayError('RESERVATION_AMOUNT_INVALID')
        if self.limit_price >= 1 or self.maximum_debit < self.quantity * self.limit_price:
            raise ReplayError('RESERVATION_BOUND_TOO_SMALL')
        positive_int(self.expires_wall_ms, 'RESERVATION_EXPIRY_INVALID')

    @property
    def key(self) -> str:
        return digest((self.experiment_id, self.protocol_hash, self.market_id, self.signal_id))

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> 'ReservationRequest':
        values = dict(raw)
        for name in ('maximum_debit', 'quantity', 'limit_price'):
            values[name] = decimal(values[name])
        return cls(**values)


@dataclass(frozen=True)
class ReservationLimits:
    currency: str
    portfolio: Decimal
    asset: Decimal
    horizon: Decimal
    strategy: Decimal
    parent_shock: Decimal
    market: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency:
            raise ReplayError('RESERVATION_CURRENCY_MISSING')
        for value in (self.portfolio, self.asset, self.horizon, self.strategy, self.parent_shock, self.market):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ReplayError('RESERVATION_LIMIT_INVALID')


class ReservationProjection:
    """Single-owner state reconstructed from canonical events across code SHAs.

    External exposures describe ALL positions/reservations outside this opt-in
    cohort, in the SAME collateral currency. Empty is only allowed with an
    explicit complete-account reconciliation assertion and checkpoint identity.
    This class never infers completeness from missing files or a stale cache.
    """
    def __init__(self, *, code_sha: str, checkpoint_id: str, starting_cash: Decimal,
                 external_exposures: Iterable[Mapping[str, Any]], limits: ReservationLimits,
                 whole_portfolio_reconciled: bool):
        if len(code_sha) != 40 or any(c not in '0123456789abcdef' for c in code_sha):
            raise ReplayError('RESERVATION_EXACT_SHA_REQUIRED')
        if (whole_portfolio_reconciled is not True or not checkpoint_id
                or not isinstance(starting_cash, Decimal) or not starting_cash.is_finite() or starting_cash < 0):
            raise ReplayError('COMPLETE_ACCOUNT_CHECKPOINT_REQUIRED')
        self.code_sha, self.checkpoint_id, self.limits = code_sha, checkpoint_id, limits
        self.cash = starting_cash
        self._external: list[dict[str, Any]] = []
        required = {'market_id', 'asset', 'horizon', 'strategy', 'parent_shock_id', 'currency', 'cost', 'state'}
        for row in external_exposures:
            if set(row) != required or row['currency'] != limits.currency or row['state'] not in ('RESERVED', 'OPEN'):
                raise ReplayError('EXTERNAL_EXPOSURE_INCOMPLETE_OR_MIXED_CURRENCY')
            if any(not isinstance(row[k], str) or not row[k] for k in required - {'cost'}):
                raise ReplayError('EXTERNAL_EXPOSURE_IDENTITY_MISSING')
            amount = decimal(row['cost'])
            if amount < 0:
                raise ReplayError('EXTERNAL_EXPOSURE_NEGATIVE')
            self._external.append(dict(row, cost=amount))
        self._requests: dict[str, ReservationRequest] = {}
        self._states: dict[str, str] = {}
        self._amounts: dict[str, Decimal] = {}
        self._shares: dict[str, Decimal] = {}
        self._records: dict[str, LedgerEvent] = {}
        self._record_hashes: dict[str, str] = {}
        self._operations: dict[tuple[str, str], LedgerEvent] = {}
        self._burned_markets: set[str] = set()
        self._poisoned = False
        self._foreign_checkpoint_stale = False
        self._active_ids: set[str] = set()
        self._fill_records_by_order: dict[str, set[str]] = {}
        self._lock = threading.RLock()  # Correctness guard; not described as lock-free.
        self._checkpoint_hash = digest({'checkpoint': checkpoint_id, 'cash': starting_cash,
                                        'external': self._external, 'limits': limits})

    def _healthy(self) -> None:
        if self._poisoned:
            raise ReplayError('AMBIGUOUS_DURABILITY_REQUIRES_CANONICAL_RECOVERY')

    def _active(self) -> list[dict[str, Any]]:
        rows = list(self._external)
        for key in sorted(self._active_ids):
            rows.append(dict(primitive(self._requests[key]), cost=self._amounts[key]))
        return rows

    def _reserved(self) -> Decimal:
        external_reserved = sum((r['cost'] for r in self._external if r['state'] == 'RESERVED'), ZERO)
        return external_reserved + sum((self._amounts[key] for key in self._active_ids
                    if self._states[key] in ('RESERVED', 'SUBMITTED')), ZERO)

    def _receipt(self, request: ReservationRequest, receipt: Mapping[str, Any]) -> None:
        if (receipt.get('owner') != OWNER or receipt.get('action') != 'TAKE'
                or receipt.get('selected_replay_key') != request.coordinator_replay_key
                or receipt.get('new_risk_authorized') is not False
                or receipt.get('paper_exploration_authorized') is not True
                or receipt.get('paper_only') is not True
                or receipt.get('authenticated_execution') is not False
                or receipt.get('real_order_submission') is not False
                or receipt.get('real_capital_at_risk') is not False):
            raise ReplayError('COORDINATOR_RECEIPT_BINDING_INVALID')

    def _event(self, request: ReservationRequest, operation: str, now_ms: int,
               details: Mapping[str, Any]) -> LedgerEvent:
        positive_int(now_ms, 'RESERVATION_WALL_CLOCK_INVALID')
        event_type = 'CAPITAL_RESERVE' if operation == 'RESERVE' else 'ORDER_STATE' if operation == 'SUBMIT_FENCE' else 'CAPITAL_RELEASE'
        metadata = {'component': 'crypto_informed_taker', 'reservation_projection': {
            'schema': SCHEMA, 'owner': OWNER, 'checkpoint_hash': self._checkpoint_hash,
            'reservation_id': request.key, 'operation': operation, 'request': primitive(request),
            'details': primitive(details)}}
        return LedgerEvent(event_type=event_type, strategy=request.strategy, model_sha=self.code_sha,
                           record_id='reservation-' + digest((request.key, operation)), recorded_ts_ms=now_ms,
                           order_id=request.key, market_id=request.market_id, token_id=request.token_id,
                           order_state='PENDING_PAPER_RECONCILIATION' if operation == 'SUBMIT_FENCE' else None,
                           metadata=metadata)

    def _commit(self, event: LedgerEvent, append: Append) -> LedgerEvent:
        self._healthy()
        # State is advanced only after synchronous canonical append succeeds.
        # An exception may occur after fsync; recovery, not a retry, decides.
        try:
            append(event)
            self.observe(event)
        except Exception:
            self._poisoned = True
            raise
        return event

    def reserve(self, request: ReservationRequest, *, now_ms: int, receipt: Mapping[str, Any],
                append: Append, entry_gate_open: bool = False) -> LedgerEvent:
        with self._lock:
            self._healthy()
            self._receipt(request, receipt)
            if self._foreign_checkpoint_stale:
                raise ReplayError('FOREIGN_MONETARY_EVENT_REQUIRES_ACCOUNT_RECONCILIATION')
            if entry_gate_open is not True:
                raise ReplayError('NEW_ENTRY_GATE_CLOSED')
            if request.currency != self.limits.currency:
                raise ReplayError('RESERVATION_CURRENCY_MISMATCH')
            if now_ms > request.expires_wall_ms:
                raise ReplayError('AUTHORIZATION_EXPIRED')
            if request.key in self._requests:
                if self._requests[request.key] != request:
                    raise ReplayError('RESERVATION_ID_REDEFINED')
                if self._states[request.key] == 'RESERVED':
                    return LedgerEvent.from_dict(self._operations[(request.key, 'RESERVE')].to_dict())
                raise ReplayError('SIGNAL_ALREADY_ATTEMPTED')
            active = self._active()
            if request.market_id in self._burned_markets or any(r['market_id'] == request.market_id for r in active):
                raise ReplayError('MARKET_ALREADY_RESERVED_OR_TRADED')
            if request.maximum_debit > self.cash - self._reserved():
                raise ReplayError('GLOBAL_CASH_LIMIT')
            if sum((r['cost'] for r in active), ZERO) + request.maximum_debit > self.limits.portfolio:
                raise ReplayError('PORTFOLIO_RISK_LIMIT')
            for dimension, bound in (('asset', self.limits.asset), ('horizon', self.limits.horizon),
                                     ('strategy', self.limits.strategy), ('parent_shock_id', self.limits.parent_shock),
                                     ('market_id', self.limits.market)):
                exposure = sum((r['cost'] for r in active if r[dimension] == getattr(request, dimension)), ZERO)
                if exposure + request.maximum_debit > bound:
                    raise ReplayError('RISK_LIMIT:' + dimension)
            return self._commit(self._event(request, 'RESERVE', now_ms, {'receipt': dict(receipt)}), append)

    def submit_fence(self, reservation_id: str, *, now_ms: int, append: Append,
                     entry_gate_open: bool = False) -> LedgerEvent:
        with self._lock:
            self._healthy()
            request = self._requests[reservation_id]
            if self._states[reservation_id] != 'RESERVED':
                raise ReplayError('SUBMIT_ALREADY_FENCED_OR_TERMINAL')
            if self._foreign_checkpoint_stale:
                raise ReplayError('FOREIGN_MONETARY_EVENT_REQUIRES_ACCOUNT_RECONCILIATION')
            if entry_gate_open is not True or now_ms > request.expires_wall_ms:
                raise ReplayError('KILL_DRAIN_OR_AUTHORIZATION_EXPIRED')
            return self._commit(self._event(request, 'SUBMIT_FENCE', now_ms, {}), append)

    def release_unsubmitted(self, reservation_id: str, *, now_ms: int, reason: str, append: Append) -> LedgerEvent:
        with self._lock:
            self._healthy()
            if self._states[reservation_id] != 'RESERVED':
                raise ReplayError('SUBMITTED_RESERVATION_CANNOT_EXPIRE_WITHOUT_RECONCILIATION')
            if not isinstance(reason, str) or not reason:
                raise ReplayError('RELEASE_REASON_REQUIRED')
            return self._commit(self._event(self._requests[reservation_id], 'RELEASE_UNSUBMITTED', now_ms,
                                           {'reason': reason}), append)

    def _fill_totals(self, request: ReservationRequest, record_ids: tuple[str, ...]) -> tuple[Decimal, Decimal]:
        if not record_ids or len(set(record_ids)) != len(record_ids):
            raise ReplayError('CANONICAL_FILL_EVIDENCE_REQUIRED')
        gross_shares, net_shares, cost = ZERO, ZERO, ZERO
        fill_ids: set[str] = set()
        for record_id in record_ids:
            event = self._records.get(record_id)
            if (event is None or event.event_type != 'FILL' or event.market_id != request.market_id
                    or event.token_id != request.token_id or event.order_id != request.key
                    or event.side != 'BUY' or event.fill_id in fill_ids):
                raise ReplayError('CANONICAL_FILL_BINDING_INVALID')
            fill_ids.add(str(event.fill_id))
            fence = self._operations[(request.key, 'SUBMIT_FENCE')]
            if decimal(event.fill_price) > request.limit_price or event.recorded_ts_ms < fence.recorded_ts_ms:
                raise ReplayError('FILL_PRICE_OR_FENCE_TIME_MISMATCH')
            self._receipt(request, event.metadata.get('coordinator_receipt') or {})
            exact = event.metadata.get('exact_paper_fill')
            if not isinstance(exact, dict) or set(exact) != {'gross_shares', 'net_shares', 'cash_debit', 'cash_fee', 'shares_fee'}:
                raise ReplayError('EXACT_FILL_ACCOUNTING_REQUIRED')
            q, net, debit, fee_cash, fee_shares = (decimal(exact[k]) for k in
                ('gross_shares', 'net_shares', 'cash_debit', 'cash_fee', 'shares_fee'))
            if (event.metadata.get('execution_evidence') != 'SIMULATED' or min(q, net, debit) <= 0
                    or min(fee_cash, fee_shares) < 0 or net != q - fee_shares
                    or (fee_cash > 0 and fee_shares > 0) or decimal(event.filled_size) != q):
                raise ReplayError('EXACT_FILL_ACCOUNTING_MISMATCH')
            # Legacy float columns are projections, checked within one sub-micro unit.
            if abs(debit - (decimal(event.fill_price) * q + fee_cash)) > Decimal('0.0000001'):
                raise ReplayError('FILL_CASH_IDENTITY_MISMATCH')
            if abs(decimal(event.fee) - fee_cash) > Decimal('0.0000001'):
                raise ReplayError('FEE_DOUBLE_COUNT_OR_MISMATCH')
            gross_shares += q
            net_shares += net
            cost += debit
        if gross_shares > request.quantity or cost > request.maximum_debit:
            raise ReplayError('FILL_EXCEEDS_RESERVATION')
        return net_shares, cost

    def complete_fak(self, reservation_id: str, *, fill_record_ids: tuple[str, ...],
                     terminal_order_record_id: str, now_ms: int, append: Append) -> LedgerEvent:
        with self._lock:
            self._healthy()
            request = self._requests[reservation_id]
            details = {'fill_record_ids': list(fill_record_ids), 'terminal_order_record_id': terminal_order_record_id}
            prior = self._operations.get((reservation_id, 'COMPLETE_FAK'))
            if prior is not None:
                if prior.metadata['reservation_projection']['details'] != details:
                    raise ReplayError('TERMINAL_ORDER_REDEFINED')
                return LedgerEvent.from_dict(prior.to_dict())
            self._verify_fak_terminal(request, details)
            return self._commit(self._event(request, 'COMPLETE_FAK', now_ms, details), append)

    def _verify_fak_terminal(self, request: ReservationRequest, details: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
        if self._states.get(request.key) != 'SUBMITTED':
            raise ReplayError('FAK_COMPLETION_REQUIRES_SUBMIT_FENCE')
        terminal = self._records.get(details['terminal_order_record_id'])
        ids = tuple(details['fill_record_ids'])
        if (terminal is None or terminal.event_type != 'ORDER_STATE' or terminal.order_id != request.key
                or terminal.market_id != request.market_id or terminal.token_id != request.token_id
                or terminal.order_state not in ('FAK_FILLED', 'FAK_PARTIAL_CANCELLED', 'FAK_UNFILLED_CANCELLED', 'FAK_REJECTED')
                or terminal.metadata.get('fill_record_ids') != list(ids)):
            raise ReplayError('CANONICAL_FAK_TERMINAL_EVIDENCE_REQUIRED')
        self._receipt(request, terminal.metadata.get('coordinator_receipt') or {})
        earliest = max((self._records[r].recorded_ts_ms for r in ids if r in self._records),
                       default=self._operations[(request.key, 'SUBMIT_FENCE')].recorded_ts_ms)
        if terminal.recorded_ts_ms < earliest:
            raise ReplayError('ORDER_TERMINAL_PRECEDES_FILL')
        # Complete coverage: no omitted partial fill can release its committed cash.
        known = self._fill_records_by_order.get(request.key, set())
        if known != set(ids):
            raise ReplayError('TERMINAL_FILL_COVERAGE_MISMATCH')
        if not ids:
            if terminal.order_state not in ('FAK_UNFILLED_CANCELLED', 'FAK_REJECTED'):
                raise ReplayError('EMPTY_FILL_IS_NOT_SUCCESS')
            return ZERO, ZERO
        if terminal.order_state not in ('FAK_FILLED', 'FAK_PARTIAL_CANCELLED'):
            raise ReplayError('POSITIVE_FILL_CANNOT_BE_NONFILL')
        return self._fill_totals(request, ids)

    def settle(self, reservation_id: str, *, final_record_id: str, now_ms: int, append: Append) -> LedgerEvent:
        with self._lock:
            self._healthy()
            prior = self._operations.get((reservation_id, 'SETTLE'))
            if prior is not None:
                if prior.metadata['reservation_projection']['details'] != {'final_record_id': final_record_id}:
                    raise ReplayError('PAYOUT_REDEFINED_REQUIRES_APPEND_ONLY_CORRECTION')
                return LedgerEvent.from_dict(prior.to_dict())
            self._verify_final(self._requests[reservation_id], final_record_id)
            return self._commit(self._event(self._requests[reservation_id], 'SETTLE', now_ms,
                                           {'final_record_id': final_record_id}), append)

    def _verify_final(self, request: ReservationRequest, record_id: str) -> Decimal:
        if self._states.get(request.key) != 'OPEN':
            raise ReplayError('SETTLEMENT_REQUIRES_OPEN_POSITION')
        event = self._records.get(record_id)
        if (event is None or event.event_type != 'FINAL' or event.order_id != request.key
                or event.market_id != request.market_id or event.token_id != request.token_id):
            raise ReplayError('CANONICAL_FINAL_BINDING_INVALID')
        self._receipt(request, event.metadata.get('coordinator_receipt') or {})
        raw = event.metadata.get('resolution_proof')
        if not isinstance(raw, dict):
            raise ReplayError('OFFICIAL_RESOLUTION_PROOF_REQUIRED')
        payload = dict(raw)
        payload['token_payouts'] = tuple((t, decimal(p)) for t, p in payload['token_payouts'])
        proof = ResolutionProof(**payload)
        if (proof.status != 'RESOLVED' or proof.market_id != request.market_id
                or proof.available_wall_ns > event.recorded_ts_ms * 1_000_000
                or request.token_id not in dict(proof.token_payouts)):
            raise ReplayError('SETTLEMENT_NOT_OBSERVED')
        completed = self._operations[(request.key, 'COMPLETE_FAK')]
        fill_keys = completed.metadata['reservation_projection']['details']['fill_record_ids']
        last_fill_ms = max(self._records[r].recorded_ts_ms for r in fill_keys)
        if proof.available_wall_ns < last_fill_ms * 1_000_000 or event.recorded_ts_ms < completed.recorded_ts_ms:
            raise ReplayError('RESOLUTION_OR_FINAL_PRECEDES_FILL')
        payout = self._shares[request.key] * dict(proof.token_payouts)[request.token_id]
        exact = event.metadata.get('exact_terminal')
        if (not isinstance(exact, dict) or set(exact) != {'payout', 'pnl'}
                or decimal(exact['payout']) != payout
                or decimal(exact['pnl']) != payout - self._amounts[request.key]
                or abs(decimal(event.realized_cashflow) - payout) > Decimal('0.0000001')
                or abs(decimal(event.final_pnl) - decimal(exact['pnl'])) > Decimal('0.0000001')
                or (event.fee is not None and decimal(event.fee) != 0)):
            raise ReplayError('SETTLEMENT_CASH_IDENTITY_MISMATCH')
        return payout

    def observe(self, event: LedgerEvent) -> None:
        """Read an already durable record. No append, filesystem or network here."""
        with self._lock:
            event = LedgerEvent.from_dict(event.to_dict())
            encoded = digest(event.to_dict())
            if event.record_id in self._records:
                if self._record_hashes[event.record_id] != encoded:
                    self._poisoned = True
                    raise ReplayError('CANONICAL_RECORD_ID_REDEFINED')
                return
            packet = event.metadata.get('reservation_projection')
            if packet is None:
                if (event.event_type in {'CAPITAL_RESERVE', 'CAPITAL_RELEASE', 'ORDER_SUBMITTED', 'FILL', 'FINAL',
                        'INVENTORY_SPLIT', 'INVENTORY_MERGE', 'INVENTORY_LIQUIDATION'}
                        and event.order_id not in self._requests):
                    self._foreign_checkpoint_stale = True
                # Late fills after terminalization are reconciliation failures, not ignored history.
                if event.event_type == 'FILL' and event.order_id in self._states and self._states[event.order_id] != 'SUBMITTED':
                    self._poisoned = True
                    raise ReplayError('FILL_OUTSIDE_FENCED_ORDER')
                self._records[event.record_id], self._record_hashes[event.record_id] = event, encoded
                if event.event_type == 'FILL':
                    self._fill_records_by_order.setdefault(str(event.order_id), set()).add(event.record_id)
                return
            if (not isinstance(packet, dict) or packet.get('schema') != SCHEMA or packet.get('owner') != OWNER
                    or packet.get('checkpoint_hash') != self._checkpoint_hash):
                raise ReplayError('RESERVATION_CHECKPOINT_OR_OWNER_MISMATCH')
            request = ReservationRequest.parse(packet['request'])
            key, operation, details = packet['reservation_id'], packet['operation'], packet['details']
            expected_type = 'CAPITAL_RESERVE' if operation == 'RESERVE' else 'ORDER_STATE' if operation == 'SUBMIT_FENCE' else 'CAPITAL_RELEASE'
            if (key != request.key or event.order_id != key or event.event_type != expected_type
                    or event.market_id != request.market_id or event.token_id != request.token_id):
                raise ReplayError('RESERVATION_EVENT_BINDING_INVALID')
            if operation == 'RESERVE':
                self._receipt(request, details['receipt'])
                if key in self._requests or request.currency != self.limits.currency:
                    raise ReplayError('DUPLICATE_RESERVATION_EVENT')
                if (request.maximum_debit > self.cash - self._reserved() or self._foreign_checkpoint_stale
                        or event.recorded_ts_ms > request.expires_wall_ms):
                    raise ReplayError('REPLAY_RESERVATION_CASH_OR_CHECKPOINT_MISMATCH')
                active = self._active()
                if request.market_id in self._burned_markets or any(r['market_id'] == request.market_id for r in active):
                    raise ReplayError('REPLAY_DUPLICATE_MARKET_RESERVATION')
                if sum((r['cost'] for r in active), ZERO) + request.maximum_debit > self.limits.portfolio:
                    raise ReplayError('REPLAY_PORTFOLIO_LIMIT')
                for dimension, bound in (('asset', self.limits.asset), ('horizon', self.limits.horizon),
                        ('strategy', self.limits.strategy), ('parent_shock_id', self.limits.parent_shock), ('market_id', self.limits.market)):
                    if sum((r['cost'] for r in active if r[dimension] == getattr(request, dimension)), ZERO) + request.maximum_debit > bound:
                        raise ReplayError('REPLAY_DIMENSION_LIMIT:' + dimension)
                self._requests[key], self._states[key], self._amounts[key] = request, 'RESERVED', request.maximum_debit
                self._active_ids.add(key)
            else:
                if self._requests.get(key) != request or (key, operation) in self._operations:
                    raise ReplayError('RESERVATION_TRANSITION_IDENTITY_INVALID')
                state = self._states[key]
                if operation == 'SUBMIT_FENCE' and state == 'RESERVED':
                    self._states[key] = 'SUBMITTED'
                elif operation == 'RELEASE_UNSUBMITTED' and state == 'RESERVED':
                    self._states[key], self._amounts[key] = 'RELEASED', ZERO
                    self._active_ids.discard(key)
                elif operation == 'COMPLETE_FAK':
                    shares, cost = self._verify_fak_terminal(request, details)
                    if cost > self.cash:
                        raise ReplayError('REPLAY_NEGATIVE_CASH')
                    self.cash -= cost
                    self._shares[key], self._amounts[key] = shares, cost
                    self._states[key] = 'OPEN' if shares > 0 else 'UNFILLED'
                    if shares == 0:
                        self._active_ids.discard(key)
                    if shares > 0:
                        self._burned_markets.add(request.market_id)
                elif operation == 'SETTLE':
                    payout = self._verify_final(request, details['final_record_id'])
                    self.cash += payout
                    self._states[key], self._amounts[key] = 'FINAL', ZERO
                    self._active_ids.discard(key)
                else:
                    raise ReplayError('RESERVATION_STATE_TRANSITION_INVALID')
            self._records[event.record_id], self._record_hashes[event.record_id] = event, encoded
            self._operations[(key, operation)] = event

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {'schema': SCHEMA, 'owner': OWNER, 'checkpoint_id': self.checkpoint_id,
                    'cash': str(self.cash), 'reserved': str(self._reserved()),
                    'committed_and_reserved': str(sum((r['cost'] for r in self._active()), ZERO)),
                    'states': dict(sorted(self._states.items())), 'burned_markets': sorted(self._burned_markets),
                    'poisoned': self._poisoned, 'external_checkpoint_stale': self._foreign_checkpoint_stale,
                    'paper_only': True, 'real_order_submission': False,
                    'entry_authority': False}
