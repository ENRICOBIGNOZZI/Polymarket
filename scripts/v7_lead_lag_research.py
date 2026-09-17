#!/usr/bin/env python3
"""Causal pooled-crypto research: offline fit and immutable residual inference.

Targets are future PM mid CHANGES, never settlement probabilities. No network,
orders, registry, canonical writes or automatic promotion. Availability times
are recorder times in one declared analysis timeline, not venue event clocks.
"""
from __future__ import annotations
from dataclasses import dataclass
import argparse
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any, Mapping, Sequence
from v7_lead_lag_replay import ASSETS, HORIZONS, ReplayError, digest, positive_int, primitive


def finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReplayError('NONFINITE_FEATURE')
    return float(value)


@dataclass(frozen=True)
class FeatureObservation:
    value: float | None
    available_ns: int
    source_event_ns: int | None
    source: str
    unit: str
    valid: bool

    def __post_init__(self) -> None:
        positive_int(self.available_ns, 'FEATURE_AVAILABILITY_INVALID')
        if self.source_event_ns is not None:
            positive_int(self.source_event_ns, 'FEATURE_SOURCE_CLOCK_INVALID')
        if not self.source or not self.unit or type(self.valid) is not bool:
            raise ReplayError('FEATURE_PROVENANCE_INVALID')
        if self.value is not None:
            finite(self.value)


def available_features(observations: Mapping[str, FeatureObservation], *, decision_ns: int,
                       maximum_age_ns: Mapping[str, int]) -> dict[str, dict[str, Any]]:
    positive_int(decision_ns, 'DECISION_CLOCK_INVALID')
    out = {}
    for name, obs in observations.items():
        if name not in maximum_age_ns:
            raise ReplayError('FEATURE_TTL_MISSING:' + name)
        positive_int(maximum_age_ns[name], 'FEATURE_TTL_INVALID', allow_zero=True)
        age = decision_ns - obs.available_ns
        reason = ('FUTURE_INFORMATION' if age < 0 else 'STALE' if age > maximum_age_ns[name]
                  else 'INVALID_SOURCE' if not obs.valid else 'MISSING' if obs.value is None else None)
        out[name] = {'value': obs.value if reason is None else None, 'age_ns': age if age >= 0 else None,
                     'missing': reason is not None, 'reason': reason, 'source': obs.source, 'unit': obs.unit,
                     'available_ns': obs.available_ns}
    return out


@dataclass(frozen=True)
class ShockPolicy:
    return_window_ns: int
    half_life_ns: int
    sigma_floor: float
    warmup_observations: int
    maximum_gap_ns: int

    def __post_init__(self) -> None:
        for value in (self.return_window_ns, self.half_life_ns, self.warmup_observations, self.maximum_gap_ns):
            positive_int(value, 'SHOCK_POLICY_INVALID')
        if finite(self.sigma_floor) <= 0 or self.maximum_gap_ns < self.return_window_ns:
            raise ReplayError('SHOCK_POLICY_INVALID')


class CausalShock:
    """Score against prior EWMA second moment at the return's own timescale.

    Calibration updates use non-overlapping endpoints. All triggers may be
    scored, but dense ticks do not overweight volatility. Gap/epoch change
    clears calibration. Missing is never zero volatility.
    """
    def __init__(self, policy: ShockPolicy):
        self.policy = policy
        self.epoch: str | None = None
        self.last_received: int | None = None
        self.last_calibration: int | None = None
        self.second_moment = 0.0
        self.count = 0

    def observe(self, *, log_return: float | None, available_ns: int, window_ns: int,
                epoch: str, valid: bool = True) -> dict[str, Any]:
        positive_int(available_ns, 'SHOCK_CLOCK_INVALID')
        if window_ns != self.policy.return_window_ns or not epoch or type(valid) is not bool:
            raise ReplayError('SHOCK_SCALE_OR_EPOCH_INVALID')
        if log_return is not None:
            finite(log_return)
        if self.last_received is not None and epoch == self.epoch and available_ns <= self.last_received:
            raise ReplayError('DUPLICATE_OR_OUT_OF_ORDER_SHOCK')
        reset = (epoch != self.epoch or not valid or log_return is None
                 or (self.last_received is not None and available_ns - self.last_received > self.policy.maximum_gap_ns))
        if reset:
            self.count, self.second_moment, self.last_calibration = 0, 0.0, None
        self.epoch, self.last_received = epoch, available_ns
        ready = self.count >= self.policy.warmup_observations and valid and log_return is not None
        sigma = max(math.sqrt(self.second_moment), self.policy.sigma_floor) if ready else None
        score = float(log_return) / sigma if sigma is not None else None
        result = {'shock_z': score, 'sigma_before_trigger': sigma, 'calibration_count_before': self.count,
                  'available_ns': available_ns, 'missing': score is None,
                  'reason': None if ready else 'VOLATILITY_WARMING_OR_GAP'}
        if valid and log_return is not None and (self.last_calibration is None
                or available_ns - self.last_calibration >= self.policy.return_window_ns):
            squared = float(log_return) ** 2
            if self.last_calibration is None:
                self.second_moment = squared
            else:
                elapsed = available_ns - self.last_calibration
                alpha = -math.expm1(-math.log(2.0) * elapsed / self.policy.half_life_ns)
                self.second_moment = (1 - alpha) * self.second_moment + alpha * squared
            self.count += 1
            self.last_calibration = available_ns
        return result


def non_opposing(primary: FeatureObservation, confirmation: FeatureObservation | None,
                 *, decision_ns: int, maximum_age_ns: int) -> tuple[bool, str]:
    if confirmation is None:
        return False, 'CONFIRMATION_MISSING'
    view = available_features({'primary': primary, 'confirmation': confirmation}, decision_ns=decision_ns,
                              maximum_age_ns={'primary': maximum_age_ns, 'confirmation': maximum_age_ns})
    if view['primary']['missing']:
        return False, 'PRIMARY_MISSING'
    if view['confirmation']['missing']:
        return False, 'CONFIRMATION_MISSING'
    if primary.unit != confirmation.unit:
        return False, 'CONFIRMATION_UNIT_MISMATCH'
    a, b = view['primary']['value'], view['confirmation']['value']
    return (True, 'NON_OPPOSING') if a * b >= 0 else (False, 'CONFIRMATION_OPPOSING')


@dataclass(frozen=True)
class ResearchRow:
    row_id: str
    asset: str
    horizon: str
    decision_ns: int
    label_end_ns: int
    label_available_ns: int
    features: tuple[float | None, ...]
    pm_mid: float
    future_mid: float | None
    market_id: str
    parent_shock_id: str
    feature_available_ns: tuple[int | None, ...]
    feature_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.row_id or not self.market_id or not self.parent_shock_id:
            raise ReplayError('RESEARCH_IDENTITY_MISSING')
        if self.asset not in ASSETS or self.horizon not in HORIZONS:
            raise ReplayError('RESEARCH_SCOPE_INVALID')
        for value in (self.decision_ns, self.label_end_ns, self.label_available_ns):
            positive_int(value, 'RESEARCH_CLOCK_INVALID')
        if not self.decision_ns < self.label_end_ns <= self.label_available_ns:
            raise ReplayError('LABEL_CLOCK_INVALID')
        for value in (self.features, self.feature_available_ns, self.feature_source_ids):
            if not isinstance(value, tuple):
                raise ReplayError('MUTABLE_FEATURE_VECTOR')
        if not len(self.features) == len(self.feature_available_ns) == len(self.feature_source_ids):
            raise ReplayError('FEATURE_PROVENANCE_SHAPE')
        for value, availability, source in zip(self.features, self.feature_available_ns, self.feature_source_ids):
            if not isinstance(source, str) or not source:
                raise ReplayError('FEATURE_SOURCE_MISSING')
            if availability is not None:
                positive_int(availability, 'FEATURE_AVAILABILITY_INVALID')
            if value is not None:
                finite(value)
                if availability is None or availability > self.decision_ns:
                    raise ReplayError('NONCAUSAL_FEATURE')
        if not 0 <= finite(self.pm_mid) <= 1:
            raise ReplayError('PM_PRIOR_INVALID')
        if self.future_mid is not None and not 0 <= finite(self.future_mid) <= 1:
            raise ReplayError('PM_LABEL_INVALID')

    @property
    def target(self) -> float | None:
        return None if self.future_mid is None else self.future_mid - self.pm_mid

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> 'ResearchRow':
        data = dict(raw)
        for name in ('features', 'feature_available_ns', 'feature_source_ids'):
            data[name] = tuple(data[name])
        return cls(**data)


def chronological_split(rows: Sequence[ResearchRow], *, train_end_ns: int,
                        validation_end_ns: int, embargo_ns: int) -> dict[str, tuple[ResearchRow, ...]]:
    """One UTC split for all assets; availability purge and shared-shock embargo.

    A market/shock crossing partitions is purged from the earlier partition,
    never removed from test to improve results. No high-frequency row shuffle.
    """
    positive_int(train_end_ns, 'SPLIT_CLOCK_INVALID')
    if type(validation_end_ns) is not int or validation_end_ns <= train_end_ns:
        raise ReplayError('SPLIT_CLOCK_INVALID')
    positive_int(embargo_ns, 'EMBARGO_INVALID', allow_zero=True)
    if len({r.row_id for r in rows}) != len(rows):
        raise ReplayError('DUPLICATE_RESEARCH_ROW')
    groups: dict[str, list[ResearchRow]] = {k: [] for k in ('train', 'validation', 'test', 'purged')}
    for row in sorted(rows, key=lambda r: (r.decision_ns, r.row_id)):
        if row.decision_ns < train_end_ns and row.label_available_ns < train_end_ns - embargo_ns:
            name = 'train'
        elif train_end_ns + embargo_ns <= row.decision_ns < validation_end_ns and row.label_available_ns < validation_end_ns - embargo_ns:
            name = 'validation'
        elif row.decision_ns >= validation_end_ns + embargo_ns:
            name = 'test'
        else:
            name = 'purged'
        groups[name].append(row)
    seen_markets, seen_shocks = set(), set()
    for name in ('test', 'validation', 'train'):
        accepted = []
        for row in groups[name]:
            if row.market_id in seen_markets or row.parent_shock_id in seen_shocks:
                groups['purged'].append(row)
            else:
                accepted.append(row)
        seen_markets.update(r.market_id for r in groups[name])
        seen_shocks.update(r.parent_shock_id for r in groups[name])
        groups[name] = accepted
    return {k: tuple(v) for k, v in groups.items()}


def _solve_spd(matrix: list[list[float]], vector: list[float]) -> tuple[float, ...]:
    """Cholesky for a small strictly regularized baseline, standard library only."""
    n = len(vector)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            residual = matrix[i][j] - math.fsum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                if residual <= 0 or not math.isfinite(residual):
                    raise ReplayError('RIDGE_SYSTEM_NOT_POSITIVE_DEFINITE')
                lower[i][j] = math.sqrt(residual)
            else:
                lower[i][j] = residual / lower[j][j]
    y = [0.0] * n
    for i in range(n):
        y[i] = (vector[i] - math.fsum(lower[i][j] * y[j] for j in range(i))) / lower[i][i]
    answer = [0.0] * n
    for i in range(n - 1, -1, -1):
        answer[i] = (y[i] - math.fsum(lower[j][i] * answer[j] for j in range(i + 1, n))) / lower[i][i]
    return tuple(answer)


@dataclass(frozen=True)
class FrozenResidualModel:
    schema: str
    feature_names: tuple[str, ...]
    units: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    training_cutoff_ns: int
    training_hash: str
    ridge_penalty: float
    label_horizon_ns: int
    model_hash: str

    def __post_init__(self) -> None:
        if self.schema != 'polymarket_v7_pooled_pm_residual_model_v1':
            raise ReplayError('MODEL_SCHEMA_MISMATCH')
        for value in (self.feature_names, self.units, self.means, self.scales, self.coefficients):
            if not isinstance(value, tuple):
                raise ReplayError('MODEL_MUST_BE_IMMUTABLE')
        p = len(self.feature_names)
        if (p == 0 or len(set(self.feature_names)) != p or len(self.units) != p
                or len(self.means) != p or len(self.scales) != p or len(self.coefficients) != 1 + 2 * p):
            raise ReplayError('MODEL_SHAPE_MISMATCH')
        if any(not isinstance(n, str) or not n for n in (*self.feature_names, *self.units)):
            raise ReplayError('MODEL_UNITS_MISSING')
        for value in (*self.means, *self.scales, *self.coefficients):
            finite(value)
        if any(s <= 0 for s in self.scales) or finite(self.ridge_penalty) <= 0:
            raise ReplayError('MODEL_SCALE_OR_REGULARIZATION_INVALID')
        positive_int(self.training_cutoff_ns, 'MODEL_CUTOFF_INVALID')
        positive_int(self.label_horizon_ns, 'MODEL_HORIZON_INVALID')
        payload = dict(self.__dict__)
        payload.pop('model_hash')
        if self.model_hash != digest(payload):
            raise ReplayError('MODEL_HASH_MISMATCH')

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> 'FrozenResidualModel':
        value = dict(raw)
        for name in ('feature_names', 'units', 'means', 'scales', 'coefficients'):
            value[name] = tuple(value[name])
        return cls(**value)

    def predict(self, values: tuple[float | None, ...], *, feature_names: tuple[str, ...],
                units: tuple[str, ...], decision_ns: int, label_horizon_ns: int) -> float:
        if feature_names != self.feature_names or units != self.units or len(values) != len(self.means):
            raise ReplayError('MODEL_FEATURE_SCHEMA_MISMATCH')
        if decision_ns <= self.training_cutoff_ns:
            raise ReplayError('MODEL_NOT_AVAILABLE_AT_DECISION')
        if label_horizon_ns != self.label_horizon_ns:
            raise ReplayError('MODEL_LABEL_HORIZON_MISMATCH')
        x = [1.0]
        for value, mean, scale in zip(values, self.means, self.scales):
            x.extend((0.0 if value is None else (finite(value) - mean) / scale, float(value is None)))
        result = math.fsum(a * b for a, b in zip(x, self.coefficients))
        if not math.isfinite(result):
            raise ReplayError('MODEL_PREDICTION_NONFINITE')
        return result


def fit_residual_model(rows: Sequence[ResearchRow], *, feature_names: tuple[str, ...],
                       units: tuple[str, ...], training_cutoff_ns: int,
                       ridge_penalty: float, label_horizon_ns: int) -> FrozenResidualModel:
    p = len(feature_names)
    if not rows or p == 0 or p > 128 or len(units) != p or len(set(feature_names)) != p:
        raise ReplayError('TRAINING_SHAPE_INVALID')
    if finite(ridge_penalty) <= 0:
        raise ReplayError('POSITIVE_RIDGE_REQUIRED')
    if len({r.row_id for r in rows}) != len(rows):
        raise ReplayError('DUPLICATE_TRAINING_ROW')
    if any(len(r.features) != p or r.label_available_ns >= training_cutoff_ns or r.target is None
           or r.label_end_ns - r.decision_ns != label_horizon_ns for r in rows):
        raise ReplayError('TRAINING_LEAKAGE_OR_MISSING_LABEL')
    rows = sorted(rows, key=lambda r: (r.decision_ns, r.row_id))
    means, scales = [], []
    for j in range(p):
        values = [float(r.features[j]) for r in rows if r.features[j] is not None]
        if not values:
            raise ReplayError('UNOBSERVED_TRAINING_FEATURE:' + feature_names[j])
        means.append(statistics.fmean(values))
        scales.append(max(statistics.pstdev(values), 1e-12))
    n = 1 + 2 * p
    gram = [[0.0] * n for _ in range(n)]
    rhs = [0.0] * n
    for row in rows:
        x = [1.0]
        for value, mean, scale in zip(row.features, means, scales):
            x.extend((0.0 if value is None else (value - mean) / scale, float(value is None)))
        for i in range(n):
            rhs[i] += x[i] * float(row.target) / len(rows)
            for j in range(n):
                gram[i][j] += x[i] * x[j] / len(rows)
    # Intercept shrinkage is intentional and versioned, not a hidden numerical fix.
    for i in range(n):
        gram[i][i] += ridge_penalty
    payload = dict(schema='polymarket_v7_pooled_pm_residual_model_v1', feature_names=feature_names,
                   units=units, means=tuple(means), scales=tuple(scales), coefficients=_solve_spd(gram, rhs),
                   training_cutoff_ns=training_cutoff_ns, training_hash=digest(rows),
                   ridge_penalty=float(ridge_penalty), label_horizon_ns=label_horizon_ns)
    return FrozenResidualModel(**payload, model_hash=digest(payload))


def cluster_mean_ci(values: Sequence[float | None], cluster_ids: Sequence[str], *,
                    draws: int = 2000, seed: int = 17, minimum_clusters: int = 20) -> dict[str, Any]:
    """Whole-block resampling; ratio of sums preserves candidate weighting."""
    if len(values) != len(cluster_ids) or any(not isinstance(c, str) or not c for c in cluster_ids):
        raise ReplayError('CLUSTER_SHAPE_INVALID')
    positive_int(draws, 'BOOTSTRAP_DRAWS_INVALID')
    if type(minimum_clusters) is not int or minimum_clusters < 2:
        raise ReplayError('BOOTSTRAP_MINIMUM_INVALID')
    groups: dict[str, list[float]] = {}
    incomplete = set()
    for value, key in zip(values, cluster_ids):
        if value is None:
            incomplete.add(key)
        else:
            groups.setdefault(key, []).append(finite(value))
    output: dict[str, Any] = {'cluster_count': len(set(cluster_ids)),
                             'complete_cluster_count': len(set(groups) - incomplete),
                             'candidate_count': len(values), 'missing_count': sum(v is None for v in values),
                             'draws': draws, 'seed': seed, 'minimum_clusters': minimum_clusters,
                             'mean': None, 'ci95': [None, None], 'status': 'INSUFFICIENT_EVIDENCE'}
    if not values or any(v is None for v in values) or len(groups) < minimum_clusters:
        return output
    aggregates = [(math.fsum(groups[k]), len(groups[k])) for k in sorted(groups)]
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sampled = [aggregates[rng.randrange(len(aggregates))] for _ in aggregates]
        means.append(math.fsum(v for v, _ in sampled) / sum(n for _, n in sampled))
    means.sort()
    def quantile(q: float) -> float:
        index = (len(means) - 1) * q
        lo, hi = math.floor(index), math.ceil(index)
        return means[lo] + (means[hi] - means[lo]) * (index - lo)
    output.update(status='ESTIMATED_CONDITIONAL_ON_BLOCK_POLICY', mean=math.fsum(values) / len(values),
                  ci95=[quantile(.025), quantile(.975)])
    return output


def connected_clusters(rows: Sequence[Mapping[str, Any]], *, block_ns: int) -> tuple[str, ...]:
    """Union common UTC block, market and shock; ticker is not an independent unit."""
    positive_int(block_ns, 'BLOCK_SIZE_INVALID')
    parents = list(range(len(rows)))
    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    seen: dict[tuple[str, Any], int] = {}
    for i, row in enumerate(rows):
        positive_int(row['decision_ns'], 'BLOCK_CLOCK_INVALID')
        for key in (('block', row['decision_ns'] // block_ns), ('market', row['market_id']),
                    ('shock', row['parent_shock_id'])):
            if key[1] in (None, ''):
                raise ReplayError('CLUSTER_ID_MISSING')
            if key in seen:
                a, b = find(i), find(seen[key])
                parents[max(a, b)] = min(a, b)
            else:
                seen[key] = i
    return tuple('cluster-' + str(find(i)) for i in range(len(rows)))


def main() -> int:
    parser = argparse.ArgumentParser(description='Fit one frozen PM-repricing ridge artifact offline')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve() or 'ledger' in args.output.parts:
        parser.exit(2, 'refusing input overwrite or canonical ledger destination\n')
    try:
        raw = json.loads(args.input.read_text())
        required = {'schema', 'evidence_type', 'feature_names', 'units', 'training_cutoff_ns',
                    'ridge_penalty', 'label_horizon_ns', 'rows'}
        if set(raw) != required or raw['schema'] != 'polymarket_v7_residual_training_dataset_v1':
            raise ReplayError('TRAINING_DATASET_SCHEMA_INVALID')
        if raw['evidence_type'] not in ('SYNTHETIC', 'RECORDED'):
            raise ReplayError('TRAINING_EVIDENCE_INVALID')
        model = fit_residual_model([ResearchRow.from_payload(r) for r in raw['rows']],
            feature_names=tuple(raw['feature_names']), units=tuple(raw['units']),
            training_cutoff_ns=raw['training_cutoff_ns'], ridge_penalty=raw['ridge_penalty'],
            label_horizon_ns=raw['label_horizon_ns'])
        result = {'schema': 'polymarket_v7_residual_training_receipt_v1',
                  'input_evidence': raw['evidence_type'], 'dataset_hash': digest(raw),
                  'model': primitive(model), 'paper_only': True, 'entry_authority': False,
                  'real_order_submission': False, 'automatic_promotion': False,
                  'economic_evidence': 'NOT_PROVEN', 'training_rows': len(raw['rows'])}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write('\n')
        print(json.dumps({'model_hash': model.model_hash, 'training_rows': len(raw['rows']),
                          'economic_evidence': 'NOT_PROVEN'}))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f'training failed closed: {type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
