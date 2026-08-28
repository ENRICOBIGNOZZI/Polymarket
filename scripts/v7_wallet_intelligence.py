#!/usr/bin/env python3
"""Causal, price-aware V7 wallet-intelligence research infrastructure.

Wallet information is deliberately a feature, never an execution authority.
Historical skill is learned only from outcomes observable before a decision
cut. Mapping verification is point-in-time and each copy/funding cluster
contributes at most one independent observation.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence


FEATURE_ONLY = True
EXECUTION_AUTHORITY = False
SCHEMA_VERSION = 2


class WalletError(ValueError):
    pass


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class MarketMapping:
    token_id: str
    market_id: str
    event_id: str
    category: str
    outcome_side: int
    condition_id: str
    question_id: str
    resolution_source: str
    rules_hash: str
    valid_from_ms: int
    verified_at_ms: int
    verifier: str
    verification_method: str
    verified: bool

    def validate(self, decision_ts_ms: int) -> None:
        if not all((self.token_id, self.market_id, self.event_id, self.category,
                    self.condition_id, self.question_id, self.resolution_source,
                    self.rules_hash, self.verifier, self.verification_method)):
            raise WalletError("mapping_identity_or_semantics_missing")
        if self.outcome_side not in {-1, 1}:
            raise WalletError("mapping_outcome_side_invalid")
        if self.valid_from_ms <= 0 or self.verified_at_ms < self.valid_from_ms:
            raise WalletError("mapping_clock_invalid")
        if self.verified_at_ms > decision_ts_ms:
            raise WalletError("mapping_not_known_at_decision")
        if not self.verified:
            raise WalletError("mapping_not_verified")
        if self.verification_method.strip().upper() in {"LLM", "TEXT_SIMILARITY"}:
            raise WalletError("mapping_verification_method_not_authoritative")

    @property
    def mapping_hash(self) -> str:
        return _canonical_hash(asdict(self))


@dataclass(frozen=True)
class RawWalletFill:
    fill_id: str
    wallet: str
    transaction_hash: str
    log_index: int
    chain_id: int
    token_id: str
    price: float
    size: float
    action: str
    block_ts_ms: int
    observed_ts_ms: int
    source: str
    source_cursor: str
    finalized: bool
    funding_cluster: str = ""

    def validate(self, as_of_ms: int) -> None:
        if not all((self.fill_id, self.wallet, self.transaction_hash, self.token_id,
                    self.source, self.source_cursor)):
            raise WalletError("fill_lineage_missing")
        if self.log_index < 0 or self.chain_id <= 0:
            raise WalletError("fill_chain_identity_invalid")
        if not 0.0 <= self.price <= 1.0 or not math.isfinite(self.size) or self.size <= 0.0:
            raise WalletError("fill_economics_invalid")
        if self.action not in {"BUY", "SELL"}:
            raise WalletError("fill_action_invalid")
        if self.block_ts_ms <= 0 or self.observed_ts_ms < self.block_ts_ms:
            raise WalletError("fill_clock_invalid")
        if self.observed_ts_ms > as_of_ms:
            raise WalletError("future_fill_used")
        if not self.finalized:
            raise WalletError("fill_not_finalized")

    @property
    def chain_key(self) -> tuple[int, str, int]:
        return (self.chain_id, self.transaction_hash, self.log_index)


@dataclass(frozen=True)
class ResolvedOutcome:
    market_id: str
    event_id: str
    outcome: int
    resolution_source: str
    rules_hash: str
    resolved_ts_ms: int
    observed_ts_ms: int
    provenance_hash: str
    verified: bool

    def validate(self, as_of_ms: int) -> None:
        if not all((self.market_id, self.event_id, self.resolution_source,
                    self.rules_hash, self.provenance_hash)):
            raise WalletError("outcome_provenance_missing")
        if self.outcome not in {0, 1}:
            raise WalletError("outcome_not_binary")
        if self.resolved_ts_ms <= 0 or self.observed_ts_ms < self.resolved_ts_ms:
            raise WalletError("outcome_clock_invalid")
        if self.observed_ts_ms > as_of_ms:
            raise WalletError("future_outcome_used")
        if not self.verified:
            raise WalletError("outcome_not_verified")


@dataclass(frozen=True)
class WalletTrade:
    wallet: str
    category: str
    market_id: str
    event_id: str
    side: int
    entry_probability: float
    outcome: float | None
    size: float
    trade_ts_ms: int
    funding_cluster: str = ""
    observed_ts_ms: int = 0
    outcome_observed_ts_ms: int = 0
    fill_id: str = ""
    mapping_hash: str = ""
    outcome_provenance_hash: str = ""

    @property
    def price_aware_edge(self) -> float:
        if self.side not in {-1, 1}:
            raise WalletError("side_must_be_signed")
        if (not 0.0 <= self.entry_probability <= 1.0
                or self.outcome not in {0.0, 1.0}):
            raise WalletError("invalid_or_unresolved_binary_trade")
        return self.side * (float(self.outcome) - self.entry_probability)


@dataclass(frozen=True)
class WalletTapeRecord:
    sequence: int
    record_type: str
    record_id: str
    effective_ts_ms: int
    observed_ts_ms: int
    payload_hash: str
    previous_hash: str
    record_hash: str


def reconstruct_trades(
    fills: Iterable[RawWalletFill], mappings: Iterable[MarketMapping],
    outcomes: Iterable[ResolvedOutcome] = (), *, as_of_ms: int,
    require_resolved: bool = False,
) -> tuple[WalletTrade, ...]:
    """Point-in-time fill reconstruction with strict semantic lineage."""
    mapping_by_token: dict[str, MarketMapping] = {}
    for mapping in mappings:
        mapping.validate(as_of_ms)
        previous = mapping_by_token.get(mapping.token_id)
        if previous is not None and previous.mapping_hash != mapping.mapping_hash:
            raise WalletError(f"conflicting_market_mapping:{mapping.token_id}")
        mapping_by_token[mapping.token_id] = mapping

    outcome_by_market: dict[str, ResolvedOutcome] = {}
    for outcome in outcomes:
        outcome.validate(as_of_ms)
        previous = outcome_by_market.get(outcome.market_id)
        if previous is not None and asdict(previous) != asdict(outcome):
            raise WalletError(f"conflicting_resolved_outcome:{outcome.market_id}")
        outcome_by_market[outcome.market_id] = outcome

    seen_chain: dict[tuple[int, str, int], RawWalletFill] = {}
    output: list[WalletTrade] = []
    for fill in sorted(fills, key=lambda x: (x.observed_ts_ms, x.chain_key, x.fill_id)):
        fill.validate(as_of_ms)
        previous = seen_chain.get(fill.chain_key)
        if previous is not None:
            if asdict(previous) != asdict(fill):
                raise WalletError(f"conflicting_chain_fill:{fill.transaction_hash}:{fill.log_index}")
            continue
        seen_chain[fill.chain_key] = fill
        mapping = mapping_by_token.get(fill.token_id)
        if mapping is None:
            raise WalletError(f"market_mapping_missing:{fill.token_id}")
        mapping.validate(fill.observed_ts_ms)
        outcome = outcome_by_market.get(mapping.market_id)
        if outcome is not None:
            if outcome.event_id != mapping.event_id or outcome.rules_hash != mapping.rules_hash:
                raise WalletError(f"outcome_mapping_lineage_mismatch:{mapping.market_id}")
            if outcome.resolved_ts_ms < fill.block_ts_ms:
                raise WalletError(f"trade_after_resolution:{fill.fill_id}")
        elif require_resolved:
            continue
        token_direction = mapping.outcome_side
        trade_direction = 1 if fill.action == "BUY" else -1
        output.append(WalletTrade(
            wallet=fill.wallet, category=mapping.category, market_id=mapping.market_id,
            event_id=mapping.event_id, side=token_direction * trade_direction,
            entry_probability=fill.price if token_direction == 1 else 1.0 - fill.price,
            outcome=float(outcome.outcome) if outcome is not None else None,
            size=fill.size, trade_ts_ms=fill.block_ts_ms,
            funding_cluster=fill.funding_cluster, observed_ts_ms=fill.observed_ts_ms,
            outcome_observed_ts_ms=outcome.observed_ts_ms if outcome is not None else 0,
            fill_id=fill.fill_id, mapping_hash=mapping.mapping_hash,
            outcome_provenance_hash=outcome.provenance_hash if outcome is not None else "",
        ))
    return tuple(output)


def build_causal_tape(
    fills: Iterable[RawWalletFill], mappings: Iterable[MarketMapping],
    outcomes: Iterable[ResolvedOutcome], *, as_of_ms: int,
) -> tuple[WalletTapeRecord, ...]:
    """Build a deterministic, append-verifiable reconstruction tape."""
    fills, mappings, outcomes = tuple(fills), tuple(mappings), tuple(outcomes)
    raw: list[tuple[int, int, int, str, str, Any]] = []
    for mapping in mappings:
        mapping.validate(as_of_ms)
        raw.append((mapping.valid_from_ms, mapping.verified_at_ms, 0, "MARKET_MAPPING",
                    mapping.token_id, asdict(mapping)))
    for fill in fills:
        fill.validate(as_of_ms)
        raw.append((fill.block_ts_ms, fill.observed_ts_ms, 1, "ONCHAIN_FILL", fill.fill_id,
                    asdict(fill)))
    for outcome in outcomes:
        outcome.validate(as_of_ms)
        raw.append((outcome.resolved_ts_ms, outcome.observed_ts_ms, 3, "RESOLVED_OUTCOME",
                    outcome.market_id, asdict(outcome)))
    # Reconstructed trades are written at fill-observation time without a
    # terminal label. The later outcome remains a separate tape fact.
    reconstructed = reconstruct_trades(fills, mappings, as_of_ms=as_of_ms)
    for trade in reconstructed:
        raw.append((trade.trade_ts_ms, trade.observed_ts_ms, 2, "MAPPED_TRADE",
                    trade.fill_id, asdict(trade)))
    raw.sort(key=lambda x: (x[1], x[2], x[3], x[4]))
    previous_hash = "0" * 64
    tape: list[WalletTapeRecord] = []
    seen_ids: dict[tuple[str, str], str] = {}
    for effective, observed, _priority, kind, record_id, payload in raw:
        payload_hash = _canonical_hash(payload)
        key = (kind, record_id)
        prior = seen_ids.get(key)
        if prior is not None:
            if prior != payload_hash:
                raise WalletError(f"conflicting_tape_record:{kind}:{record_id}")
            continue
        seen_ids[key] = payload_hash
        sequence = len(tape) + 1
        record_hash = _canonical_hash({"sequence": sequence, "record_type": kind,
                                       "record_id": record_id, "effective_ts_ms": effective,
                                       "observed_ts_ms": observed, "payload_hash": payload_hash,
                                       "previous_hash": previous_hash})
        tape.append(WalletTapeRecord(sequence, kind, record_id, effective, observed,
                                     payload_hash, previous_hash, record_hash))
        previous_hash = record_hash
    return tuple(tape)


@dataclass(frozen=True)
class CategoryPrior:
    category: str
    mean_edge: float
    std_edge: float
    independent_clusters: int
    trained_until_ms: int


@dataclass(frozen=True)
class SkillPosterior:
    wallet: str
    category: str
    observations: int
    raw_mean_edge: float
    posterior_mean_edge: float
    posterior_std: float
    lower_95: float
    effective_observations: float = 0.0
    trained_until_ms: int = 0


def _independent_clusters(rows: Sequence[WalletTrade]) -> list[list[WalletTrade]]:
    grouped: dict[tuple[str, str], list[WalletTrade]] = {}
    for row in rows:
        grouped.setdefault((row.event_id, row.market_id), []).append(row)
    return list(grouped.values())


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    total = sum(weights)
    if total <= 0.0:
        raise WalletError("non_positive_sample_weight")
    return sum(value * weight for value, weight in zip(values, weights)) / total


def estimate_skill(
    trades: Iterable[WalletTrade], *, category_prior_mean: float = 0.0,
    category_prior_std: float = 0.05, observation_std_floor: float = 0.05,
    as_of_ms: int | None = None, skill_half_life_days: float | None = None,
    category_prior: CategoryPrior | None = None,
) -> SkillPosterior:
    rows = tuple(trades)
    if not rows:
        raise WalletError("wallet_skill_requires_observations")
    identity = {(x.wallet, x.category) for x in rows}
    if len(identity) != 1:
        raise WalletError("mixed_wallet_or_category")
    if as_of_ms is not None:
        rows = tuple(x for x in rows if x.trade_ts_ms <= as_of_ms
                     and (x.outcome_observed_ts_ms <= 0 or x.outcome_observed_ts_ms <= as_of_ms))
        if not rows:
            raise WalletError("wallet_skill_requires_observable_outcomes")
    if category_prior is not None:
        wallet, category = next(iter(identity))
        if category_prior.category != category:
            raise WalletError("category_prior_mismatch")
        if as_of_ms is not None and category_prior.trained_until_ms >= as_of_ms:
            raise WalletError("category_prior_training_leakage")
        category_prior_mean = category_prior.mean_edge
        category_prior_std = category_prior.std_edge
    if category_prior_std <= 0.0 or observation_std_floor <= 0.0:
        raise WalletError("invalid_category_prior")
    if skill_half_life_days is not None and skill_half_life_days <= 0.0:
        raise WalletError("invalid_skill_half_life")

    clusters = _independent_clusters(rows)
    edges = [statistics.fmean(x.price_aware_edge for x in group) for group in clusters]
    cluster_times = [max((x.outcome_observed_ts_ms or x.trade_ts_ms) for x in group)
                     for group in clusters]
    reference = as_of_ms if as_of_ms is not None else max(cluster_times)
    weights = ([1.0] * len(edges) if skill_half_life_days is None else [
        math.exp(-math.log(2.0) * max(0, reference - ts)
                 / (skill_half_life_days * 86_400_000.0)) for ts in cluster_times])
    raw = _weighted_mean(edges, weights)
    effective_n = sum(weights)
    weighted_var = (_weighted_mean([(x - raw) ** 2 for x in edges], weights)
                    if len(edges) > 1 else observation_std_floor ** 2)
    variance_of_mean = max(observation_std_floor ** 2, weighted_var) / effective_n
    prior_var = category_prior_std ** 2
    posterior_var = 1.0 / (1.0 / prior_var + 1.0 / variance_of_mean)
    posterior_mean = posterior_var * (category_prior_mean / prior_var + raw / variance_of_mean)
    posterior_std = math.sqrt(posterior_var)
    wallet, category = next(iter(identity))
    return SkillPosterior(wallet, category, len(edges), raw, posterior_mean, posterior_std,
                          posterior_mean - 1.96 * posterior_std, effective_n,
                          max(cluster_times))


def copy_clusters(trades: Sequence[WalletTrade], *, timing_window_ms: int = 2_000) -> tuple[tuple[str, ...], ...]:
    """Deterministic conservative clustering by fund origin and repeated timing."""
    if timing_window_ms < 0:
        raise WalletError("invalid_copy_timing_window")
    wallets = sorted({x.wallet for x in trades})
    adjacency = {wallet: {wallet} for wallet in wallets}
    by_wallet: dict[str, list[WalletTrade]] = {wallet: [] for wallet in wallets}
    for row in trades:
        by_wallet[row.wallet].append(row)
    for index, left in enumerate(wallets):
        for right in wallets[index + 1:]:
            left_rows, right_rows = by_wallet[left], by_wallet[right]
            same_funder = bool({x.funding_cluster for x in left_rows if x.funding_cluster}.intersection(
                {x.funding_cluster for x in right_rows if x.funding_cluster}))
            coincident_markets = {
                (a.event_id, a.market_id) for a in left_rows
                if any(a.event_id == b.event_id and a.market_id == b.market_id
                       and a.side == b.side
                       and abs(a.trade_ts_ms - b.trade_ts_ms) <= timing_window_ms
                       for b in right_rows)
            }
            # Split fills in a single market do not constitute repeated copy
            # behaviour; require coincidence in two independent contracts.
            if same_funder or len(coincident_markets) >= 2:
                adjacency[left].add(right)
                adjacency[right].add(left)
    seen: set[str] = set()
    clusters: list[tuple[str, ...]] = []
    for wallet in wallets:
        if wallet in seen:
            continue
        stack, component = [wallet], set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency[node] - component)
        seen.update(component)
        clusters.append(tuple(sorted(component)))
    return tuple(sorted(clusters))


def fit_category_priors(
    trades: Sequence[WalletTrade], *, as_of_ms: int, std_floor: float = 0.025,
) -> dict[str, CategoryPrior]:
    """Fit empirical priors using one value per independent copy cluster."""
    if std_floor <= 0.0:
        raise WalletError("invalid_prior_std_floor")
    observable = [row for row in trades if row.outcome is not None and row.trade_ts_ms < as_of_ms
                  and (row.outcome_observed_ts_ms <= 0 or row.outcome_observed_ts_ms < as_of_ms)]
    result: dict[str, CategoryPrior] = {}
    for category in sorted({x.category for x in observable}):
        rows = [x for x in observable if x.category == category]
        values: list[float] = []
        for cluster in copy_clusters(rows):
            members = set(cluster)
            cluster_rows = [x for x in rows if x.wallet in members]
            event_edges = [statistics.fmean(y.price_aware_edge for y in group)
                           for group in _independent_clusters(cluster_rows)]
            if event_edges:
                values.append(statistics.fmean(event_edges))
        if not values:
            continue
        mean = statistics.fmean(values)
        std = max(std_floor, statistics.stdev(values) if len(values) > 1 else std_floor)
        trained_until = max((x.outcome_observed_ts_ms or x.trade_ts_ms) for x in rows)
        result[category] = CategoryPrior(category, mean, std, len(values), trained_until)
    return result


@dataclass(frozen=True)
class DatasetManifest:
    schema_version: int
    created_at_ms: int
    as_of_ms: int
    row_count: int
    first_trade_ts_ms: int
    last_trade_ts_ms: int
    mapping_hashes: tuple[str, ...]
    outcome_provenance_hashes: tuple[str, ...]
    dataset_hash: str
    feature_only: bool = FEATURE_ONLY
    execution_authority: bool = EXECUTION_AUTHORITY


def historical_dataset(
    fills: Iterable[RawWalletFill], mappings: Iterable[MarketMapping],
    outcomes: Iterable[ResolvedOutcome], *, as_of_ms: int, created_at_ms: int,
) -> tuple[tuple[WalletTrade, ...], DatasetManifest]:
    if created_at_ms < as_of_ms:
        raise WalletError("manifest_created_before_dataset_cut")
    trades = reconstruct_trades(fills, mappings, outcomes, as_of_ms=as_of_ms,
                                require_resolved=True)
    rows = tuple(sorted(trades, key=lambda x: (x.trade_ts_ms, x.market_id, x.wallet, x.fill_id)))
    manifest = DatasetManifest(
        schema_version=SCHEMA_VERSION, created_at_ms=created_at_ms, as_of_ms=as_of_ms,
        row_count=len(rows), first_trade_ts_ms=min((x.trade_ts_ms for x in rows), default=0),
        last_trade_ts_ms=max((x.trade_ts_ms for x in rows), default=0),
        mapping_hashes=tuple(sorted({x.mapping_hash for x in rows})),
        outcome_provenance_hashes=tuple(sorted({x.outcome_provenance_hash for x in rows})),
        dataset_hash=_canonical_hash([asdict(row) for row in rows]),
    )
    return rows, manifest


@dataclass(frozen=True)
class OosPrediction:
    wallet: str
    category: str
    market_id: str
    decision_ts_ms: int
    trained_until_ms: int
    training_observations: int
    predicted_edge: float
    realized_edge: float
    qualified: bool
    feature_only: bool = FEATURE_ONLY
    execution_authority: bool = EXECUTION_AUTHORITY


def incremental_oos(
    trades: Sequence[WalletTrade], *, minimum_training_observations: int = 2,
    skill_half_life_days: float = 90.0, minimum_lower_skill: float = 0.0,
) -> tuple[OosPrediction, ...]:
    """Expanding chronological OOS: only previously observed labels train a cut."""
    if minimum_training_observations <= 0:
        raise WalletError("minimum_training_observations_invalid")
    resolved = [x for x in trades if x.outcome is not None and x.outcome_observed_ts_ms > 0]
    output: list[OosPrediction] = []
    for test in sorted(resolved, key=lambda x: (x.trade_ts_ms, x.market_id, x.wallet, x.fill_id)):
        training = [x for x in resolved if x.outcome_observed_ts_ms < test.trade_ts_ms]
        wallet_training = [x for x in training if x.wallet == test.wallet and x.category == test.category]
        if len(_independent_clusters(wallet_training)) < minimum_training_observations:
            continue
        prior = fit_category_priors(training, as_of_ms=test.trade_ts_ms).get(test.category)
        posterior = estimate_skill(wallet_training, as_of_ms=test.trade_ts_ms,
                                   skill_half_life_days=skill_half_life_days,
                                   category_prior=prior)
        output.append(OosPrediction(
            test.wallet, test.category, test.market_id, test.trade_ts_ms,
            posterior.trained_until_ms, posterior.observations,
            test.side * posterior.posterior_mean_edge, test.price_aware_edge,
            posterior.lower_95 > minimum_lower_skill,
        ))
    return tuple(output)


@dataclass(frozen=True)
class WalletSignal:
    category: str
    signed_signal: float
    independent_clusters: int
    contributing_wallets: int
    feature_only: bool = FEATURE_ONLY
    execution_authority: bool = EXECUTION_AUTHORITY


def aggregate_flow(
    current_trades: Sequence[WalletTrade], posteriors: Mapping[tuple[str, str], SkillPosterior],
    *, minimum_lower_skill: float = 0.0, half_life_seconds: float = 3_600.0,
    now_ms: int,
) -> WalletSignal:
    if not current_trades:
        return WalletSignal("", 0.0, 0, 0)
    if half_life_seconds <= 0.0:
        raise WalletError("invalid_flow_half_life")
    categories = {x.category for x in current_trades}
    if len(categories) != 1:
        raise WalletError("flow_signal_requires_one_category")
    if any(x.trade_ts_ms > now_ms for x in current_trades):
        raise WalletError("future_flow_trade_used")
    clusters = copy_clusters(current_trades)
    by_wallet: dict[str, list[WalletTrade]] = {}
    for trade in current_trades:
        by_wallet.setdefault(trade.wallet, []).append(trade)
    contributions: list[float] = []
    contributing: set[str] = set()
    for cluster in clusters:
        cluster_values: list[float] = []
        for wallet in cluster:
            for trade in by_wallet.get(wallet, ()):
                posterior = posteriors.get((wallet, trade.category))
                if posterior is None or posterior.lower_95 <= minimum_lower_skill:
                    continue
                age_s = max(0.0, (now_ms - trade.trade_ts_ms) / 1_000.0)
                recency = math.exp(-math.log(2.0) * age_s / half_life_seconds)
                conviction = math.log1p(trade.size)
                cluster_values.append(trade.side * posterior.posterior_mean_edge * conviction * recency)
                contributing.add(wallet)
        if cluster_values:
            contributions.append(statistics.fmean(cluster_values))
    return WalletSignal(next(iter(categories)), sum(contributions), len(contributions),
                        len(contributing))


@dataclass(frozen=True)
class WalletFeatureVector:
    category: str
    fair_value_logit_shift: float
    toxicity: float
    market_selection_score: float
    flow_context: float
    qualified_wallets: int
    independent_clusters: int
    generated_at_ms: int
    feature_only: bool = FEATURE_ONLY
    execution_authority: bool = EXECUTION_AUTHORITY


def forward_features(
    current_trades: Sequence[WalletTrade], posteriors: Mapping[tuple[str, str], SkillPosterior],
    *, now_ms: int, minimum_lower_skill: float = 0.0,
    half_life_seconds: float = 3_600.0, max_abs_logit_shift: float = 0.25,
) -> WalletFeatureVector:
    """Expose bounded context for fair/toxicity/selection; never an action."""
    if max_abs_logit_shift <= 0.0:
        raise WalletError("invalid_feature_bound")
    signal = aggregate_flow(current_trades, posteriors,
                            minimum_lower_skill=minimum_lower_skill,
                            half_life_seconds=half_life_seconds, now_ms=now_ms)
    bounded = max(-max_abs_logit_shift, min(max_abs_logit_shift, signal.signed_signal))
    return WalletFeatureVector(
        category=signal.category, fair_value_logit_shift=bounded,
        toxicity=abs(bounded),
        market_selection_score=1.0 - math.exp(-signal.independent_clusters),
        flow_context=signal.signed_signal, qualified_wallets=signal.contributing_wallets,
        independent_clusters=signal.independent_clusters, generated_at_ms=now_ms,
    )


__all__ = [
    "CategoryPrior", "DatasetManifest", "EXECUTION_AUTHORITY", "FEATURE_ONLY",
    "MarketMapping", "OosPrediction", "RawWalletFill", "ResolvedOutcome",
    "SCHEMA_VERSION", "SkillPosterior", "WalletError", "WalletFeatureVector",
    "WalletSignal", "WalletTapeRecord", "WalletTrade", "aggregate_flow",
    "build_causal_tape", "copy_clusters", "estimate_skill", "fit_category_priors",
    "forward_features", "historical_dataset", "incremental_oos", "reconstruct_trades",
]
