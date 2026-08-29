#!/usr/bin/env python3
from __future__ import annotations

import math
from typing import Hashable, Mapping, TypeVar

K = TypeVar("K", bound=Hashable)


def harmonic_number(m: int) -> float:
    n = max(0, int(m))
    return sum(1.0 / k for k in range(1, n + 1))


def by_effective_q(q: float, m: int) -> float:
    n = max(0, int(m))
    if n <= 0:
        return 0.0
    level = min(1.0, max(0.0, float(q)))
    return level / harmonic_number(n)


def by_selected(pvalues: Mapping[K, float], q: float) -> set[K]:
    """Benjamini-Yekutieli step-up selection under arbitrary dependence.

    The mapping must contain the *entire pre-declared hypothesis family*. A
    structurally declared hypothesis that cannot be estimated on the available
    price panel must remain in the family with p=1 rather than disappearing after
    data inspection. This keeps the multiplicity denominator fixed ex ante.
    """
    rows = sorted(
        ((key, min(1.0, max(0.0, float(value)))) for key, value in pvalues.items()),
        key=lambda item: (item[1], repr(item[0])),
    )
    m = len(rows)
    if m <= 0:
        return set()
    q_by = by_effective_q(q, m)
    cutoff_rank = 0
    for rank, (_key, pvalue) in enumerate(rows, start=1):
        if pvalue <= q_by * rank / m + 1e-15:
            cutoff_rank = rank
    return {key for key, _pvalue in rows[:cutoff_rank]}


def by_resolution_diagnostics(hypotheses: int, reps: int, q: float) -> dict[str, float | int | bool]:
    """Check plus-one Monte-Carlo resolution against the first BY threshold."""
    m = max(0, int(hypotheses))
    b = max(1, int(reps))
    level = min(1.0, max(1e-12, float(q)))
    h_m = harmonic_number(m) if m else 0.0
    effective_q = level / h_m if h_m > 0.0 else 0.0
    minimum_attainable = 1.0 / (b + 1.0)
    first_threshold = effective_q / m if m > 0 else 0.0
    required_reps = max(0, math.ceil(1.0 / first_threshold) - 1) if first_threshold > 0.0 else 0
    minimum_rank = (
        max(1, math.ceil(minimum_attainable * m / effective_q))
        if m > 0 and effective_q > 0.0
        else 0
    )
    return {
        "hypotheses": m,
        "repetitions": b,
        "harmonic_number": h_m,
        "nominal_fdr_q": level,
        "by_effective_q": effective_q,
        "minimum_attainable_pvalue": minimum_attainable,
        "first_rank_by_threshold": first_threshold,
        "singleton_by_resolution_adequate": bool(m > 0 and minimum_attainable <= first_threshold),
        "repetitions_required_for_singleton_by_resolution": required_reps,
        "minimum_rank_needed_if_pvalues_hit_nominal_floor": minimum_rank,
    }
