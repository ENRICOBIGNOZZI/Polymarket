#!/usr/bin/env python3
from __future__ import annotations

import json
import random
from typing import Iterable


def quantile_lower(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty bootstrap sample")
    index = min(len(ordered) - 1, max(0, int(quantile * (len(ordered) - 1))))
    return ordered[index]


def iid_bootstrap_lower(values: list[float], *, seed: int, reps: int, quantile: float) -> float:
    rng = random.Random(seed)
    n = len(values)
    means = [sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps)]
    return quantile_lower(means, quantile)


def nonoverlap_block_bootstrap_lower(
    values: list[float], *, block_length: int, seed: int, reps: int, quantile: float
) -> float:
    if block_length <= 0 or len(values) % block_length:
        raise ValueError("counterexample expects complete non-overlapping blocks")
    blocks = [values[start:start + block_length] for start in range(0, len(values), block_length)]
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(reps):
        sample: list[float] = []
        for _ in range(len(blocks)):
            sample.extend(blocks[rng.randrange(len(blocks))])
        means.append(sum(sample) / len(sample))
    return quantile_lower(means, quantile)


def deterministic_counterexample() -> dict[str, float | int | bool | str]:
    # Four persistent regimes, ten adjacent sessions each. Treating 40 rows as
    # independent makes the lower bound positive; resampling whole regimes makes
    # the lower bound negative. This is a dependence counterexample, not a model
    # of the actual Polymarket regime process.
    values = [1.0] * 10 + [1.0] * 10 + [0.5] * 10 + [-1.0] * 10
    seed = 20260826
    reps = 20_000
    quantile = 0.10
    iid_lower = iid_bootstrap_lower(values, seed=seed, reps=reps, quantile=quantile)
    block_lower = nonoverlap_block_bootstrap_lower(
        values, block_length=10, seed=seed, reps=reps, quantile=quantile
    )
    return {
        "sessions": len(values),
        "chronological_blocks": 4,
        "block_length_sessions": 10,
        "mean_pnl": sum(values) / len(values),
        "iid_bootstrap_lower_10pct": iid_lower,
        "block_bootstrap_lower_10pct": block_lower,
        "iid_accepts_positive_lower": iid_lower > 0.0,
        "block_rejects_positive_lower": block_lower <= 0.0,
        "finding": "iid session bootstrap can overstate confidence when adjacent Graph/RV sessions are serially dependent",
    }


if __name__ == "__main__":
    print(json.dumps(deterministic_counterexample(), indent=2, sort_keys=True))
