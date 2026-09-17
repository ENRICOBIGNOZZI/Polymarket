#!/usr/bin/env python3
"""Offline common-pool latency/capacity replay with one simulated cash balance.

Decision-time reservation, arrival-time FAK, and modeled redemption are
separate events. This program never reads secrets, places orders, writes the
canonical ledger, or grants runtime authority. Even recorded input produces
SIMULATED fills, not exchange execution evidence.
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import replace
from decimal import Decimal
import hashlib
import heapq
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from v7_lead_lag_replay import (ArrivalCut, ArrivalTape, BookCut, Clock, DepthDepletion, FeeTerms,
    FillResult, LatencyScenario, ReplayError, ReplayIntent, ResolutionProof, ZERO, arrival_rejection,
    decimal, digest, positive_int, primitive, settlement_pnl, simulate_arrival)
from v7_lead_lag_research import cluster_mean_ci, connected_clusters

SCHEMA = 'polymarket_v7_lead_lag_replay_dataset_v1'
DEFAULT_LATENCIES_MS = (0, 5, 10, 25, 50, 100, 250, 500, 1000)
DEFAULT_SIZES = ('5', '10', '25', '50', '100', '250', '500')


def parse_intent(raw: Mapping[str, Any]) -> ReplayIntent:
    data = dict(raw)
    data['clock'] = Clock(**data['clock'])
    for name in ('quantity', 'limit_price', 'tick'):
        data[name] = decimal(data[name])
    return ReplayIntent(**data)


def parse_cut(raw: Mapping[str, Any]) -> ArrivalCut:
    data = dict(raw)
    book = dict(data.pop('book'))
    book['clock'] = Clock(**book['clock'])
    for name in ('tick', 'min_size'):
        book[name] = decimal(book[name])
    for name in ('bids', 'asks'):
        book[name] = tuple((decimal(p), decimal(q)) for p, q in book[name])
    fee = dict(data.pop('fee'))
    for name in ('rate', 'quantum', 'share_quantum'):
        if name in fee:
            fee[name] = decimal(fee[name])
    return ArrivalCut(book=BookCut(**book), fee=FeeTerms(**fee), **data)


def parse_proof(raw: Mapping[str, Any]) -> ResolutionProof:
    data = dict(raw)
    data['token_payouts'] = tuple((t, decimal(p)) for t, p in data['token_payouts'])
    return ResolutionProof(**data)


def validate_dataset(raw: Mapping[str, Any]) -> tuple[list[ReplayIntent], list[ArrivalCut], dict[str, ResolutionProof]]:
    expected = {'schema', 'evidence_type', 'identity', 'source_files', 'candidates', 'cuts',
                'resolutions', 'as_of_wall_ns', 'analysis_policy'}
    if set(raw) != expected or raw['schema'] != SCHEMA:
        raise ReplayError('DATASET_SCHEMA_MISMATCH')
    if raw['evidence_type'] not in ('SYNTHETIC', 'RECORDED'):
        raise ReplayError('DATASET_EVIDENCE_TYPE_INVALID')
    identity = raw['identity']
    required = {'code_sha', 'experiment_id', 'protocol_hash', 'config_hash', 'feature_schema_hash',
                'model_hash', 'fill_model_hash', 'cost_model_hash', 'settlement_semantic_hash',
                'latency_profile_id', 'run_id', 'data_cutoff_wall_ns'}
    if not isinstance(identity, dict) or set(identity) != required:
        raise ReplayError('DATASET_LINEAGE_INCOMPLETE')
    if not re.fullmatch('[0-9a-f]{40}', str(identity['code_sha'])):
        raise ReplayError('EXACT_CODE_SHA_REQUIRED')
    for key in required:
        if key.endswith('_hash') and not re.fullmatch('[0-9a-f]{64}', str(identity[key])):
            raise ReplayError('DATASET_HASH_INVALID:' + key)
    for key in ('run_id', 'experiment_id', 'latency_profile_id'):
        if not isinstance(identity[key], str) or not identity[key]:
            raise ReplayError('DATASET_LINEAGE_INCOMPLETE')
    positive_int(identity['data_cutoff_wall_ns'], 'DATA_CUTOFF_INVALID')
    positive_int(raw['as_of_wall_ns'], 'REPORT_CUTOFF_INVALID')
    intents = [parse_intent(x) for x in raw['candidates']]
    if len({i.trace_id for i in intents}) != len(intents) or len({i.attempt_id for i in intents}) != len(intents):
        raise ReplayError('CANDIDATE_POOL_NOT_DEDUPLICATED')
    for intent in intents:
        if intent.experiment_id != identity['experiment_id'] or intent.protocol_hash != identity['protocol_hash']:
            raise ReplayError('MIXED_EXPERIMENT_OR_PROTOCOL')
        if not identity['data_cutoff_wall_ns'] < intent.clock.wall_ns <= raw['as_of_wall_ns']:
            raise ReplayError('CANDIDATE_OUTSIDE_PROSPECTIVE_WINDOW')
        if intent.side != 'BUY':
            raise ReplayError('REPORT_REQUIRES_HOLD_TO_SETTLEMENT_ENTRIES')
    if len({(i.clock.host_id, i.clock.boot_id) for i in intents}) > 1:
        raise ReplayError('MULTIPLE_CLOCK_DOMAINS_REQUIRE_SEPARATE_REPLAY')
    # Redemption-to-local-time mapping requires a coherent recorded clock anchor.
    if len({i.clock.wall_ns - i.clock.monotonic_ns for i in intents}) > 1:
        raise ReplayError('CLOCK_STEP_REQUIRES_SEGREGATED_REPLAY')
    cuts = [parse_cut(x) for x in raw['cuts']]
    proofs: dict[str, ResolutionProof] = {}
    for value in raw['resolutions']:
        p = parse_proof(value)
        if p.market_id in proofs:
            raise ReplayError('DUPLICATE_RESOLUTION_REQUIRES_EXPLICIT_CORRECTION')
        proofs[p.market_id] = p
    policy = raw['analysis_policy']
    policy_keys = {'processing_ns', 'network_ns', 'depth_fraction', 'block_ns', 'minimum_clusters',
                   'bootstrap_draws', 'bootstrap_seed', 'require_initial_full_depth', 'currency',
                   'shared_cash', 'portfolio_loss_cap', 'asset_loss_cap', 'horizon_loss_cap',
                   'shock_loss_cap', 'modeled_redemption_delay_ns'}
    if not isinstance(policy, dict) or set(policy) != policy_keys:
        raise ReplayError('ANALYSIS_POLICY_INCOMPLETE')
    for key in ('processing_ns', 'modeled_redemption_delay_ns'):
        positive_int(policy[key], 'ANALYSIS_POLICY_INVALID:' + key, allow_zero=True)
    for key in ('network_ns', 'block_ns', 'minimum_clusters', 'bootstrap_draws'):
        positive_int(policy[key], 'ANALYSIS_POLICY_INVALID:' + key)
    if policy['minimum_clusters'] < 2 or type(policy['bootstrap_seed']) is not int or type(policy['require_initial_full_depth']) is not bool:
        raise ReplayError('ANALYSIS_POLICY_INVALID')
    for key in ('shared_cash', 'portfolio_loss_cap', 'asset_loss_cap', 'horizon_loss_cap', 'shock_loss_cap'):
        if decimal(policy[key]) <= 0:
            raise ReplayError('ANALYSIS_BUDGET_INVALID:' + key)
    if not isinstance(policy['currency'], str) or not policy['currency']:
        raise ReplayError('ANALYSIS_CURRENCY_INVALID')
    if any(c.fee.currency != policy['currency'] for c in cuts):
        raise ReplayError('MIXED_COLLATERAL_CURRENCIES')
    return intents, cuts, proofs


def source_manifest(raw: Mapping[str, Any], *, base: Path) -> list[dict[str, Any]]:
    out = []
    if raw['evidence_type'] == 'RECORDED' and not raw['source_files']:
        raise ReplayError('RECORDED_SOURCES_REQUIRED')
    for entry in raw['source_files']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}:
            raise ReplayError('SOURCE_MANIFEST_INVALID')
        path = (base / entry['path']).resolve()
        h = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                h.update(chunk)
        if h.hexdigest() != entry['sha256']:
            raise ReplayError('SOURCE_HASH_MISMATCH:' + entry['path'])
        out.append({'path': entry['path'], 'sha256': h.hexdigest(), 'bytes': path.stat().st_size})
    return out


def run_scenario(intents: Sequence[ReplayIntent], tape: ArrivalTape, proofs: Mapping[str, ResolutionProof],
                 *, scenario: LatencyScenario, quantity: Decimal | None, as_of_wall_ns: int,
                 policy: Mapping[str, Any]) -> dict[str, Any]:
    """Event-driven counterfactual; decision reservations cannot spend the same cash."""
    if quantity is not None and (not quantity.is_finite() or quantity <= 0):
        raise ReplayError('SCENARIO_SIZE_INVALID')
    candidates = {i.trace_id: replace(i, quantity=quantity) if quantity is not None else i for i in intents}
    depletion = DepthDepletion()
    cash = decimal(policy['shared_cash'])
    reservations: dict[str, Decimal] = {}
    positions: dict[str, dict[str, Any]] = {}
    traded: set[str] = set()
    results: dict[str, FillResult] = {}
    rows: dict[str, dict[str, Any]] = {}
    peak_capital = ZERO
    state_unknown = False
    # Stable tie policy: redemption, arrival, then decision. No future book at decision.
    queue = [(i.clock.monotonic_ns, 2, i.trace_id) for i in candidates.values()]
    heapq.heapify(queue)
    offset = next(iter(candidates.values())).clock.wall_ns - next(iter(candidates.values())).clock.monotonic_ns if candidates else 0
    cutoff = as_of_wall_ns - offset

    def empty(i: ReplayIntent, status: str, reason: str) -> FillResult:
        cut = tape.at(i, scenario.arrival(i))
        return FillResult(i.trace_id, status, reason, scenario.arrival(i),
                          cut.book.snapshot_id if cut else None, i.quantity,
                          ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ())

    def occupied() -> list[dict[str, Any]]:
        out = []
        for key, amount in reservations.items():
            i = candidates[key]
            out.append({'market_id': i.market_id, 'asset': i.asset, 'horizon': i.horizon,
                        'parent_shock_id': i.parent_shock_id, 'cost': amount})
        out.extend(p for p in positions.values() if not p['released'])
        return out

    while queue:
        when, phase, key = heapq.heappop(queue)
        if when > cutoff:
            break
        intent = candidates[key]
        if phase == 0:
            position = positions[key]
            p = proofs[intent.market_id]
            if not position['released']:
                cash += position['shares'] * dict(p.token_payouts)[intent.token_id]
                position['released'] = True
            continue
        if phase == 2:
            reason = 'PRIOR_EXECUTION_UNKNOWN' if state_unknown else None
            decision = tape.at(intent, intent.clock)
            if reason is None:
                reason = 'DECISION_BOOK_UNOBSERVED' if decision is None else arrival_rejection(intent, decision, intent.clock)
            if reason is None and decision.book.snapshot_id != intent.decision_snapshot_id:
                reason = 'DECISION_SNAPSHOT_MISMATCH'
            if reason is None and (not decision.book.asks or decision.book.asks[0][0] != intent.limit_price):
                reason = 'DECISION_NO_CHASE_LIMIT_MISMATCH'
            if reason is None and policy['require_initial_full_depth']:
                depth = sum((q for p, q in decision.book.asks if p <= intent.limit_price), ZERO)
                if depth < intent.quantity:
                    reason = 'INITIAL_DEPTH_INSUFFICIENT'
            active = occupied()
            if reason is None and (intent.market_id in traded or any(p['market_id'] == intent.market_id for p in active)):
                reason = 'MARKET_ALREADY_RESERVED_OR_TRADED'
            bound = ZERO
            if reason is None:
                fee = decision.fee
                fee_bound = ZERO
                if fee.incidence == 'CASH':
                    # Covers any possible price-level partition within the fixed limit.
                    maximum_levels = int(intent.limit_price / intent.tick) + 1
                    fee_bound = intent.quantity * fee.rate * Decimal('.25') ** fee.exponent + fee.quantum * maximum_levels
                bound = intent.quantity * intent.limit_price + fee_bound
                if bound > cash - sum(reservations.values(), ZERO):
                    reason = 'GLOBAL_CASH_LIMIT'
                elif sum((p['cost'] for p in active), ZERO) + bound > decimal(policy['portfolio_loss_cap']):
                    reason = 'PORTFOLIO_RISK_LIMIT'
                else:
                    for dimension, cap in (('asset', 'asset_loss_cap'), ('horizon', 'horizon_loss_cap'), ('parent_shock_id', 'shock_loss_cap')):
                        used = sum((p['cost'] for p in active if p[dimension] == getattr(intent, dimension)), ZERO)
                        if used + bound > decimal(policy[cap]):
                            reason = 'RISK_LIMIT:' + dimension
                            break
            if reason is not None:
                unknown = reason in {'DECISION_BOOK_UNOBSERVED', 'DECISION_SNAPSHOT_MISMATCH',
                    'CLOCK_DOMAIN_MISMATCH', 'PM_BOOK_UNSYNCED', 'PM_BOOK_INVALID', 'PM_BOOK_TOO_OLD',
                    'FEES_UNKNOWN', 'PRIOR_EXECUTION_UNKNOWN'}
                result = empty(intent, 'UNKNOWN' if unknown else 'REJECTED', reason)
                state_unknown = state_unknown or unknown
                results[key] = result
            else:
                reservations[key] = bound
                heapq.heappush(queue, (scenario.arrival(intent).monotonic_ns, 1, key))
                peak_capital = max(peak_capital, sum((p['cost'] for p in occupied()), ZERO))
            continue
        # Arrival: revalidation may reject, partially fill, or become unobservable.
        result = simulate_arrival(intent, tape.at(intent, scenario.arrival(intent)), scenario, depletion)
        results[key] = result
        if result.status == 'UNKNOWN':
            state_unknown = True
            # Keep the maximum reservation. Never infer a zero-cost crash recovery.
            continue
        reserved = reservations.pop(key)
        if result.filled > 0:
            cost = -result.cash_delta
            if cost > reserved or cost > cash:
                raise ReplayError('REPLAY_CASH_RECONCILIATION_FAILED')
            cash -= cost
            traded.add(intent.market_id)
            positions[key] = {'market_id': intent.market_id, 'token_id': intent.token_id,
                'asset': intent.asset, 'horizon': intent.horizon, 'parent_shock_id': intent.parent_shock_id,
                'cost': cost, 'shares': result.net_shares, 'released': False}
            p = proofs.get(intent.market_id)
            if p is not None and p.status == 'RESOLVED':
                if intent.token_id not in dict(p.token_payouts) or p.available_wall_ns < result.arrival_clock.wall_ns:
                    raise ReplayError('RESOLUTION_BINDING_OR_TIME_INVALID')
                redeem = p.available_wall_ns + policy['modeled_redemption_delay_ns'] - offset
                heapq.heappush(queue, (redeem, 0, key))
    for key, intent in candidates.items():
        result = results.get(key) or empty(intent, 'UNKNOWN', 'ARRIVAL_PENDING_AT_REPORT_CUTOFF')
        pnl = settlement_pnl(intent, result, proofs.get(intent.market_id), as_of_wall_ns=as_of_wall_ns)
        rows[key] = {'trace_id': key, 'market_id': intent.market_id, 'asset': intent.asset,
                    'horizon': intent.horizon, 'parent_shock_id': intent.parent_shock_id,
                    'decision_ns': intent.clock.wall_ns, 'status': result.status, 'reason': result.reason,
                    'fill': primitive(result), 'pnl': None if pnl is None else str(pnl)}
    ordered = [rows[key] for key in sorted(rows)]
    values = [None if r['pnl'] is None else decimal(r['pnl']) for r in ordered]
    known = [v for v in values if v is not None]
    clusters = connected_clusters(ordered, block_ns=policy['block_ns'])
    return {'scenario': primitive(scenario), 'counterfactual_quantity': None if quantity is None else str(quantity),
            'candidate_count': len(ordered), 'filled_candidates': sum(decimal(r['fill']['filled']) > 0 for r in ordered),
            'unknown_candidates': sum(r['status'] == 'UNKNOWN' for r in ordered),
            'pending_or_unknown_pnl_count': sum(v is None for v in values),
            'known_pnl_contribution': str(sum(known, ZERO)),
            'total_pnl': str(sum(known, ZERO)) if len(known) == len(values) else None,
            'mean_pnl_per_candidate': str(sum(known, ZERO) / len(values)) if values and len(known) == len(values) else None,
            'maximum_committed_and_reserved_capital': str(peak_capital),
            'cash_as_of_report': str(cash), 'reserved_cash_as_of_report': str(sum(reservations.values(), ZERO)),
            'unredeemed_positions': sum(not p['released'] for p in positions.values()),
            'reason_counts': dict(sorted(Counter(r['reason'] for r in ordered).items())),
            'cluster_mean_ci': cluster_mean_ci([None if v is None else float(v) for v in values], clusters,
                draws=policy['bootstrap_draws'], seed=policy['bootstrap_seed'], minimum_clusters=policy['minimum_clusters']),
            'rows': ordered}


def build_report(raw: Mapping[str, Any], *, latencies_ms: Sequence[int], sizes: Sequence[Decimal],
                 verified_sources: Sequence[Mapping[str, Any]], report_code_sha: str) -> dict[str, Any]:
    intents, cuts, proofs = validate_dataset(raw)
    if not re.fullmatch('[0-9a-f]{40}', report_code_sha):
        raise ReplayError('REPORT_CODE_SHA_REQUIRED')
    if raw['evidence_type'] == 'RECORDED' and not verified_sources:
        raise ReplayError('RECORDED_SOURCES_NOT_VERIFIED')
    if not latencies_ms or any(type(n) is not int or n < 0 for n in latencies_ms) or len(set(latencies_ms)) != len(latencies_ms):
        raise ReplayError('LATENCY_GRID_INVALID')
    if not sizes or any(not isinstance(s, Decimal) or not s.is_finite() or s <= 0 for s in sizes) or len(set(sizes)) != len(sizes):
        raise ReplayError('CAPACITY_GRID_INVALID')
    policy = raw['analysis_policy']
    tape = ArrivalTape(cuts)
    base = dict(profile_id=raw['identity']['latency_profile_id'], processing_ns=policy['processing_ns'],
                network_ns=policy['network_ns'], depth_fraction=decimal(policy['depth_fraction']))
    latency = [run_scenario(intents, tape, proofs, scenario=LatencyScenario(**base, additional_ns=n * 1_000_000),
                quantity=None, as_of_wall_ns=raw['as_of_wall_ns'], policy=policy) for n in latencies_ms]
    capacity = [run_scenario(intents, tape, proofs, scenario=LatencyScenario(**base, additional_ns=0),
                quantity=q, as_of_wall_ns=raw['as_of_wall_ns'], policy=policy) for q in sizes]
    common_known = set.intersection(*({r['trace_id'] for r in s['rows'] if r['pnl'] is not None} for s in latency))
    baseline = {r['trace_id']: r for r in latency[0]['rows']}
    for result in latency:
        common = [decimal(r['pnl']) for r in result['rows'] if r['trace_id'] in common_known]
        result['common_known_candidate_count'] = len(common)
        result['common_known_pnl'] = str(sum(common, ZERO))
        differences = [None if r['pnl'] is None or baseline[r['trace_id']]['pnl'] is None
                       else float(decimal(r['pnl']) - decimal(baseline[r['trace_id']]['pnl'])) for r in result['rows']]
        result['paired_delta_vs_first_latency_ci'] = cluster_mean_ci(differences,
            connected_clusters(result['rows'], block_ns=policy['block_ns']), draws=policy['bootstrap_draws'],
            seed=policy['bootstrap_seed'], minimum_clusters=policy['minimum_clusters'])
    report = {'schema': 'polymarket_v7_lead_lag_replay_report_v1', 'report_code_sha': report_code_sha,
        'input_code_sha': raw['identity']['code_sha'], 'identity': raw['identity'], 'dataset_hash': digest(raw),
        'analysis_policy_hash': digest({'policy': policy, 'latencies_ms': list(latencies_ms), 'sizes': list(sizes)}),
        'source_files': list(verified_sources), 'input_evidence': raw['evidence_type'],
        'execution_evidence': 'SIMULATED', 'economic_evidence': 'NOT_PROVEN',
        'paper_only': True, 'authenticated_execution': False, 'real_order_submission': False,
        'real_capital_at_risk': False, 'automatic_promotion': False, 'entry_authority': False,
        'as_of_wall_ns': raw['as_of_wall_ns'], 'latency_scenarios': latency, 'capacity_scenarios': capacity,
        'assumptions': ['Network transit is a declared scenario, not measured one-way latency.',
            'Received public books do not prove matching-engine state at arrival.',
            'Shared cash is reserved at decision; unknown arrival keeps that reservation.',
            'No replenishment credit for consumed depth; counterfactual impact is not identified.',
            'Per-price-level fee rounding does not identify individual exchange matches.',
            'Redemption is modeled after resolution availability plus the stated delay.',
            'Capacity sizes are counterfactual, not permission to increase active size.',
            'Source hashes verify source bytes, not independent correctness of normalization.',
            'Cluster intervals depend on the frozen block/shock policy; no annualization.']}
    report['report_hash'] = digest(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report-code-sha', required=True)
    parser.add_argument('--latencies-ms', default=','.join(map(str, DEFAULT_LATENCIES_MS)))
    parser.add_argument('--sizes', default=','.join(DEFAULT_SIZES))
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve() or 'ledger' in args.output.parts:
        parser.exit(2, 'refusing input overwrite or canonical ledger destination\n')
    try:
        raw = json.loads(args.input.read_text())
        sources = source_manifest(raw, base=args.input.resolve().parent)
        report = build_report(raw, latencies_ms=[int(v) for v in args.latencies_ms.split(',')],
            sizes=[decimal(v) for v in args.sizes.split(',')], verified_sources=sources, report_code_sha=args.report_code_sha)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write('\n')
        print(json.dumps({'report_hash': report['report_hash'], 'candidate_count': len(raw['candidates']),
                          'input_evidence': raw['evidence_type'], 'economic_evidence': 'NOT_PROVEN'}))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f'replay report failed closed: {type(exc).__name__}: {exc}\n')

if __name__ == '__main__':
    raise SystemExit(main())
