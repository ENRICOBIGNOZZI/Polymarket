#!/usr/bin/env python3
"""Price-aware, shrinkage-controlled V7 wallet information research."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


class WalletError(ValueError):
    pass


@dataclass(frozen=True)
class WalletTrade:
    wallet: str
    category: str
    market_id: str
    event_id: str
    side: int
    entry_probability: float
    outcome: float
    size: float
    trade_ts_ms: int
    funding_cluster: str = ""

    @property
    def price_aware_edge(self) -> float:
        if self.side not in {-1, 1}: raise WalletError("side_must_be_signed")
        if not 0.0 <= self.entry_probability <= 1.0 or self.outcome not in {0.0, 1.0}:
            raise WalletError("invalid_binary_trade")
        return self.side * (self.outcome - self.entry_probability)


@dataclass(frozen=True)
class SkillPosterior:
    wallet: str
    category: str
    observations: int
    raw_mean_edge: float
    posterior_mean_edge: float
    posterior_std: float
    lower_95: float


def _independent_clusters(rows: Sequence[WalletTrade]) -> list[list[WalletTrade]]:
    grouped: dict[tuple[str, str], list[WalletTrade]] = {}
    for row in rows:
        grouped.setdefault((row.event_id, row.market_id), []).append(row)
    return list(grouped.values())


def estimate_skill(
    trades: Iterable[WalletTrade], *, category_prior_mean: float = 0.0,
    category_prior_std: float = 0.05, observation_std_floor: float = 0.05,
) -> SkillPosterior:
    rows = tuple(trades)
    if not rows: raise WalletError("wallet_skill_requires_observations")
    identity = {(x.wallet, x.category) for x in rows}
    if len(identity) != 1: raise WalletError("mixed_wallet_or_category")
    clusters = _independent_clusters(rows)
    edges = [statistics.fmean(x.price_aware_edge for x in group) for group in clusters]
    raw = statistics.fmean(edges)
    sample_var = statistics.variance(edges) if len(edges) > 1 else observation_std_floor ** 2
    variance_of_mean = max(observation_std_floor ** 2, sample_var) / len(edges)
    prior_var = category_prior_std ** 2
    if prior_var <= 0.0: raise WalletError("invalid_category_prior")
    posterior_var = 1.0 / (1.0 / prior_var + 1.0 / variance_of_mean)
    posterior_mean = posterior_var * (category_prior_mean / prior_var + raw / variance_of_mean)
    posterior_std = math.sqrt(posterior_var)
    wallet, category = next(iter(identity))
    return SkillPosterior(wallet, category, len(edges), raw, posterior_mean, posterior_std,
                          posterior_mean - 1.96 * posterior_std)


def copy_clusters(trades: Sequence[WalletTrade], *, timing_window_ms: int = 2_000) -> tuple[tuple[str, ...], ...]:
    """Deterministic conservative clustering by fund origin and repeated timing."""
    wallets = sorted({x.wallet for x in trades})
    adjacency = {w: {w} for w in wallets}
    by_wallet: dict[str, list[WalletTrade]] = {w: [] for w in wallets}
    for row in trades: by_wallet[row.wallet].append(row)
    for i, left in enumerate(wallets):
        for right in wallets[i + 1:]:
            lrows, rrows = by_wallet[left], by_wallet[right]
            same_funder = bool({x.funding_cluster for x in lrows if x.funding_cluster}.intersection(
                {x.funding_cluster for x in rrows if x.funding_cluster}))
            coincident = 0
            for a in lrows:
                if any(a.market_id == b.market_id and a.side == b.side and abs(a.trade_ts_ms - b.trade_ts_ms) <= timing_window_ms for b in rrows):
                    coincident += 1
            repeated_copy = coincident >= 2
            if same_funder or repeated_copy:
                adjacency[left].add(right); adjacency[right].add(left)
    seen: set[str] = set(); clusters: list[tuple[str, ...]] = []
    for wallet in wallets:
        if wallet in seen: continue
        stack, component = [wallet], set()
        while stack:
            node = stack.pop()
            if node in component: continue
            component.add(node); stack.extend(adjacency[node] - component)
        seen.update(component); clusters.append(tuple(sorted(component)))
    return tuple(sorted(clusters))


@dataclass(frozen=True)
class WalletSignal:
    category: str
    signed_signal: float
    independent_clusters: int
    contributing_wallets: int
    feature_only: bool = True
    execution_authority: bool = False


def aggregate_flow(
    current_trades: Sequence[WalletTrade], posteriors: Mapping[tuple[str, str], SkillPosterior],
    *, minimum_lower_skill: float = 0.0, half_life_seconds: float = 3_600.0,
    now_ms: int,
) -> WalletSignal:
    if not current_trades: return WalletSignal("", 0.0, 0, 0)
    categories = {x.category for x in current_trades}
    if len(categories) != 1: raise WalletError("flow_signal_requires_one_category")
    clusters = copy_clusters(current_trades)
    by_wallet = {x.wallet: x for x in current_trades}
    contributions: list[float] = []
    contributing: set[str] = set()
    for cluster in clusters:
        cluster_values: list[float] = []
        for wallet in cluster:
            trade = by_wallet.get(wallet); posterior = posteriors.get((wallet, trade.category)) if trade else None
            if trade is None or posterior is None or posterior.lower_95 <= minimum_lower_skill: continue
            age_s = max(0.0, (now_ms - trade.trade_ts_ms) / 1_000.0)
            recency = math.exp(-math.log(2.0) * age_s / max(1e-9, half_life_seconds))
            conviction = math.log1p(max(0.0, trade.size))
            cluster_values.append(trade.side * posterior.posterior_mean_edge * conviction * recency)
            contributing.add(wallet)
        if cluster_values:
            # One copy cluster contributes at most one independent observation.
            contributions.append(statistics.fmean(cluster_values))
    return WalletSignal(next(iter(categories)), sum(contributions), len(contributions), len(contributing))


__all__ = ["SkillPosterior", "WalletError", "WalletSignal", "WalletTrade", "aggregate_flow",
           "copy_clusters", "estimate_skill"]
