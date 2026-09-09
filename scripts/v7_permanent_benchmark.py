#!/usr/bin/env python3
"""Offline model comparisons over verified permanent settlement dataset views.

Nested chronological contract partitions, train-only preprocessing, immutable
candidate freeze, and an untouched final audit. This tool never promotes models.
"""
import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import statistics

from v7_evidence_store import AUTH, canonical, digest, immutable
from v7_external_rich_train import fit, score

RIDGES = (0.1, 1.0, 10.0)
IMPLEMENTATION_BYTES = {name: (Path(__file__).parent / name).read_bytes() for name in
                       ('v7_permanent_benchmark.py', 'v7_external_rich_train.py', 'v7_external_rich_model.py')}
IMPLEMENTATION = {name: digest(raw) for name, raw in IMPLEMENTATION_BYTES.items()}


def contracts(rows):
    starts = {}
    outcomes = {}
    for row in rows:
        key = row['market_id']
        if key in starts and starts[key] != row['market_start_ms']:
            raise ValueError('conflicting contract start')
        if key in outcomes and outcomes[key] != row['actual']:
            raise ValueError('conflicting contract outcome')
        starts[key] = row['market_start_ms']; outcomes[key] = row['actual']
    return sorted(starts, key=lambda key: (starts[key], key)), starts


def partitions(rows):
    keys, starts = contracts(rows)
    if len(keys) < 80: raise ValueError('MINIMUM_80_VERIFIED_CONTRACTS')
    boundary = int(len(keys) * .8)
    audit_ids = set(keys[boundary:]); audit_start = starts[keys[boundary]]
    development = [r for r in rows if r['market_id'] not in audit_ids and r['label_received_ms'] < audit_start]
    audit = [r for r in rows if r['market_id'] in audit_ids]
    dev_keys, dev_starts = contracts(development); folds = []
    for left, right in ((.4, .6), (.6, .8), (.8, 1.0)):
        a, b = int(len(dev_keys)*left), int(len(dev_keys)*right)
        train_ids, valid_ids = set(dev_keys[:a]), set(dev_keys[a:b])
        next_start = dev_starts[dev_keys[b]] if b < len(dev_keys) else audit_start
        train = [r for r in development if r['market_id'] in train_ids and r['label_received_ms'] < dev_starts[dev_keys[a]]]
        validation = [r for r in development if r['market_id'] in valid_ids and r['label_received_ms'] < next_start]
        if len(contracts(train)[0]) < 20 or len(contracts(validation)[0]) < 8:
            raise ValueError('INSUFFICIENT_EMBARGOED_INNER_PARTITIONS')
        folds.append((train, validation))
    return development, audit, folds


def stored_score(rows, name):
    by_contract = defaultdict(list); excluded = 0
    for row in rows:
        probability = row['market_probability'] if name == 'pm' else row.get('stored_predictions', {}).get(name)
        if not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            excluded += 1; continue
        y = row['actual']; pm = row['market_probability']; p = min(1-1e-9, max(1e-9, probability))
        by_contract[row['market_id']].append(((probability-y)**2, (pm-y)**2,
                                            -y*math.log(p)-(1-y)*math.log(1-p)))
    means = [tuple(statistics.fmean(x[i] for x in values) for i in range(3)) for values in by_contract.values()]
    return {'contracts': len(means), 'rows': sum(map(len, by_contract.values())), 'missing_predictions': excluded,
            'brier': statistics.fmean(x[0] for x in means) if means else None,
            'paired_pm_brier': statistics.fmean(x[1] for x in means) if means else None,
            'brier_improvement_over_paired_pm': statistics.fmean(x[1]-x[0] for x in means) if means else None,
            'log_loss': statistics.fmean(x[2] for x in means) if means else None}


def benchmark(rows, freeze):
    keys, _ = contracts(rows)
    result = {'contracts': len(keys), 'snapshots': len(rows), 'promotion_allowed': False,
              'stored_prediction_scope': 'CAUSALLY_RECORDED_PREDICTIONS; MISSING_VALUES_EXCLUDED_WITH_PAIRED_PM',
              'prediction_identity_strata': dict(Counter(digest(canonical(r.get('prediction_identities', {}))) for r in rows))}
    try: development, audit, folds = partitions(rows)
    except ValueError as exc:
        return {**result, 'state': 'INSUFFICIENT_SAMPLE', 'reason': str(exc),
                'descriptive_stored_models': {name: stored_score(rows, name) for name in ('pm', 'rich_research', 'structural', 'hybrid')}}
    candidates = []; failures = []
    for ridge in RIDGES:
        scores = []
        try:
            for train, validation in folds:
                parameters = fit(train, ridge, 'market')
                scores.append({'model': score(validation, parameters), 'pm': score(validation, None),
                               'training_contracts': contracts(train)[0], 'validation_contracts': contracts(validation)[0],
                               'training_parameter_hash': digest(canonical(parameters))})
        except ValueError as exc:
            failures.append({'ridge': ridge, 'reason': str(exc)}); continue
        mass = sum(s['model']['contracts'] for s in scores)
        improvement = sum((s['pm']['brier']-s['model']['brier'])*s['model']['contracts'] for s in scores)/mass
        candidates.append({'ridge': ridge, 'inner_brier_improvement': improvement, 'folds': scores})
    # Candidate choice cannot inspect audit results. A deterministic tie-break
    # prefers stronger regularization. PM remains the baseline if no gain exists.
    selected = max(candidates, key=lambda c: (c['inner_brier_improvement'], c['ridge'])) if candidates else None
    parameters = fit(development, selected['ridge'], 'market') if selected and selected['inner_brier_improvement'] > 0 else None
    candidate = {'schema': 'polymarket_v7_offline_candidate_freeze_v1', **AUTH,
                 'role': 'RESEARCH_ONLY_REQUIRES_NEW_FORWARD_WINDOW', 'promotion_allowed': False,
                 'candidate_family': 'REGULARIZED_PM_RESIDUAL' if parameters else 'PM_BASELINE',
                 'parameters': parameters, 'training_contracts': contracts(development)[0],
                 'training_last_label_ms': max(r['label_received_ms'] for r in development),
                 'audit_contracts': contracts(audit)[0], 'audit_start_ms': min(r['market_start_ms'] for r in audit),
                 'implementation_hashes': IMPLEMENTATION, 'selection': selected}
    frozen_hash = freeze(candidate)  # Persist BEFORE evaluating the audit period.
    return {**result, 'state': 'OFFLINE_AUDIT_COMPLETE_FORWARD_REQUIRED', 'frozen_candidate_hash': frozen_hash,
            'inner_candidates': candidates, 'fit_failures': failures,
            'audit': {'selected_candidate': score(audit, parameters),
                      'stored_models': {name: stored_score(audit, name) for name in ('pm', 'rich_research', 'structural', 'hybrid')}},
            'nonlinear': {'state': 'NOT_RUN', 'reason': 'PREDECLARED_REQUIREMENT_2000_CONTRACTS_AND_30_DAYS' if
                          len(keys) < 2000 or len({r['market_start_ms']//86400000 for r in rows}) < 30 else
                          'REQUIRES_SEPARATELY_FROZEN_NONLINEAR_PROTOCOL_BEFORE_FUTURE_AUDIT'},
            'split': 'OUTER_80_20_WHOLE_CONTRACT; THREE_EXPANDING_INNER_FOLDS; LABEL_AVAILABILITY_EMBARGO'}


def run(manifest_path, output):
    raw = manifest_path.read_bytes(); manifest = json.loads(raw)
    if digest(raw) != manifest_path.stem or manifest.get('schema') != 'polymarket_v7_permanent_research_dataset_v1':
        raise ValueError('unverified permanent dataset manifest')
    if any(manifest.get(k) != v for k, v in AUTH.items()): raise ValueError('dataset authority mismatch')
    for name, code in IMPLEMENTATION_BYTES.items():
        immutable(output/'implementation_sources'/(IMPLEMENTATION[name]+'.py.gz'), gzip.compress(code, mtime=0))
    root = manifest_path.parent.parent; groups = defaultdict(list); seen = {}
    for part in manifest['datasets']['settlement_prediction']['parts']:
        path = root / part['path']
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()): raise ValueError('unsafe dataset chunk')
        payload = gzip.decompress(path.read_bytes())
        if digest(payload) != part['sha256']: raise ValueError('dataset chunk checksum mismatch')
        for line in payload.splitlines():
            row = json.loads(line); key = (row['source_code_sha'], row['forecast_id']); sha = digest(canonical(row))
            if key in seen:
                if seen[key] != sha: raise ValueError('conflicting forecast in dataset')
                continue
            seen[key] = sha; groups[row['feature_schema_hash']].append(row)
    def freeze(candidate):
        candidate = {**candidate, 'source_dataset_manifest_hash': manifest_path.stem}
        encoded = canonical(candidate); sha = digest(encoded); immutable(output/'candidates'/(sha+'.json'), encoded)
        return sha
    report = {'schema': 'polymarket_v7_permanent_offline_benchmark_v1', **AUTH,
              'source_dataset_manifest_hash': manifest_path.stem, 'implementation_hashes': IMPLEMENTATION,
              'cohorts': {key: benchmark(rows, freeze) for key, rows in sorted(groups.items())},
              'automatic_promotion': False}
    encoded = canonical(report); sha = digest(encoded); immutable(output/'reports'/(sha+'.json'), encoded)
    return {'report_hash': sha, **report}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--dataset-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True); args = parser.parse_args()
    print(json.dumps(run(args.dataset_manifest, args.output)))
