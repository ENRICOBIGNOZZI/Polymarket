#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import asdict, dataclass
from typing import Sequence


@dataclass(frozen=True)
class ReversionFit:
    gamma: float
    phi: float
    iid_t: float
    observations: int


@dataclass(frozen=True)
class ReversionDiagnostic:
    gamma: float
    phi: float
    iid_t: float
    bootstrap_critical_5pct: float
    incumbent_pass: bool
    bootstrap_pass: bool
    observations: int
    block_length: int
    bootstrap_reps: int


def incumbent_fit(values: Sequence[float]) -> ReversionFit:
    """Reproduce the current B2 residual AR(1) significance calculation."""
    if len(values) < 24:
        raise ValueError("at least 24 residual observations are required")

    lag = [float(x) for x in values[:-1]]
    delta = [float(values[i] - values[i - 1]) for i in range(1, len(values))]
    n = len(lag)
    mean_lag = sum(lag) / n
    mean_delta = sum(delta) / n
    sxx = sum((x - mean_lag) ** 2 for x in lag)
    if sxx < 1e-12:
        raise ValueError("degenerate lag variance")
    sxy = sum((x - mean_lag) * (y - mean_delta) for x, y in zip(lag, delta))
    gamma = sxy / sxx
    intercept = mean_delta - gamma * mean_lag
    rss = sum((y - (intercept + gamma * x)) ** 2 for x, y in zip(lag, delta))
    sigma2 = rss / max(1, n - 2)
    se = math.sqrt(max(0.0, sigma2) / sxx)
    iid_t = gamma / se if se > 1e-12 else 0.0
    return ReversionFit(gamma=gamma, phi=1.0 + gamma, iid_t=iid_t, observations=len(values))


def _default_block_length(n_differences: int) -> int:
    return max(4, min(32, int(round(max(1, n_differences) ** (1.0 / 3.0))) * 2))


def moving_block_unit_root_critical(
    values: Sequence[float],
    *,
    reps: int = 399,
    block_length: int | None = None,
    seed: int = 773,
) -> tuple[float, int]:
    """Estimate a one-sided 5% unit-root critical value by block bootstrapping differences.

    Under the null, residual levels are integrated. Centered first differences are resampled
    in circular blocks so short-memory serial dependence in innovations is retained. Each
    resample is reintegrated and scored with the incumbent t statistic. This is a research
    diagnostic, not a production stationarity test or a claim of calibrated p-values.
    """
    if reps < 99:
        raise ValueError("use at least 99 bootstrap replications")
    if len(values) < 24:
        raise ValueError("at least 24 residual observations are required")

    differences = [float(values[i] - values[i - 1]) for i in range(1, len(values))]
    mean_difference = sum(differences) / len(differences)
    innovations = [x - mean_difference for x in differences]
    n = len(innovations)
    block = block_length or _default_block_length(n)
    block = max(2, min(block, n))

    rng = random.Random(seed)
    statistics: list[float] = []
    for _ in range(reps):
        sampled: list[float] = []
        while len(sampled) < n:
            start = rng.randrange(n)
            for offset in range(block):
                sampled.append(innovations[(start + offset) % n])
                if len(sampled) == n:
                    break
        path = [0.0]
        for innovation in sampled:
            path.append(path[-1] + innovation)
        statistics.append(incumbent_fit(path).iid_t)

    statistics.sort()
    index = max(0, min(len(statistics) - 1, int(math.floor(0.05 * (len(statistics) - 1)))))
    return statistics[index], block


def diagnose_reversion(
    values: Sequence[float],
    *,
    min_iid_t: float = 1.75,
    reps: int = 399,
    block_length: int | None = None,
    seed: int = 773,
) -> ReversionDiagnostic:
    fit = incumbent_fit(values)
    critical, block = moving_block_unit_root_critical(
        values,
        reps=reps,
        block_length=block_length,
        seed=seed,
    )
    shape_ok = fit.gamma < 0.0 and 0.02 < fit.phi < 0.999
    incumbent_pass = shape_ok and fit.iid_t <= -abs(min_iid_t)
    bootstrap_pass = shape_ok and fit.iid_t <= critical
    return ReversionDiagnostic(
        gamma=fit.gamma,
        phi=fit.phi,
        iid_t=fit.iid_t,
        bootstrap_critical_5pct=critical,
        incumbent_pass=incumbent_pass,
        bootstrap_pass=bootstrap_pass,
        observations=fit.observations,
        block_length=block,
        bootstrap_reps=reps,
    )


def synthetic_unit_root(*, seed: int = 51, n: int = 672, innovation_rho: float = 0.65) -> list[float]:
    """Deterministic integrated fixture with serially correlated innovations."""
    rng = random.Random(seed)
    innovation = 0.0
    values = [0.0]
    for _ in range(1, n):
        innovation = innovation_rho * innovation + rng.gauss(0.0, 1.0)
        values.append(values[-1] + innovation)
    return values


def synthetic_stationary(*, seed: int = 1, n: int = 672, phi: float = 0.90) -> list[float]:
    """Deterministic stationary AR(1) fixture used as a positive control."""
    rng = random.Random(seed)
    values = [0.0]
    for _ in range(1, n):
        values.append(phi * values[-1] + rng.gauss(0.0, 1.0))
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Research diagnostic for B2 residual mean-reversion significance under a unit-root null"
    )
    parser.add_argument("--reps", type=int, default=399)
    parser.add_argument("--block-length", type=int)
    parser.add_argument("--seed", type=int, default=773)
    args = parser.parse_args()

    unit_root = diagnose_reversion(
        synthetic_unit_root(),
        reps=args.reps,
        block_length=args.block_length,
        seed=args.seed,
    )
    stationary = diagnose_reversion(
        synthetic_stationary(),
        reps=args.reps,
        block_length=args.block_length,
        seed=args.seed,
    )
    payload = {
        "research_state": "MORE_EVIDENCE_REQUIRED",
        "production_contract": {
            "current_iid_t_threshold": -1.75,
            "issue": (
                "The current B2 gamma/se statistic is compared with a standard-t style cutoff even though "
                "the null phi=1 has a nonstandard unit-root distribution and prediction-market innovations "
                "can be serially dependent."
            ),
        },
        "unit_root_fixture": asdict(unit_root),
        "stationary_positive_control": asdict(stationary),
        "required_next_evidence": [
            "run the incumbent and a robust unit-root/stationarity gate on identical chronological B2 residual histories",
            "use event/time-clustered or block-resampled inference and correct for the candidate search multiplicity",
            "compare candidate counts, hedge turnover and maker/taker executable edge under normal, 1.5x and 2x costs",
            "retain only changes with positive purged OOS portfolio utility versus the incumbent",
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
