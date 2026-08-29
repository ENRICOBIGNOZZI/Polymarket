#!/usr/bin/env python3
from __future__ import annotations

import math
import statistics
from typing import Mapping, Sequence


def _normalize(values: Sequence[float]) -> list[float] | None:
    norm = math.sqrt(sum(float(x) * float(x) for x in values))
    if not math.isfinite(norm) or norm <= 1e-12:
        return None
    return [float(x) / norm for x in values]


def orientation_invariant_pc1(
    controls: Mapping[str, Sequence[float]],
    *,
    max_iterations: int = 200,
    tolerance: float = 1e-11,
) -> tuple[float, ...] | None:
    """Return the first temporal PC score, invariant to control sign flips.

    Controls are rows and time points are columns.  The temporal Gram matrix
    G=X'X is unchanged when any control row is multiplied by -1, so its leading
    eigenvector is invariant to arbitrary YES/NO orientation of individual
    controls.  The sign of the returned score is canonicalized deterministically.
    """
    rows = [tuple(float(x) for x in controls[key]) for key in sorted(controls)]
    if len(rows) < 2:
        return None
    n = len(rows[0])
    if n < 4 or any(len(row) != n for row in rows):
        return None
    if not all(math.isfinite(x) for row in rows for x in row):
        return None

    # Work with X'X implicitly: G v = sum_i x_i (x_i' v).  Starting from the
    # diagonal energy vector is itself invariant to row sign flips and avoids a
    # coding-dependent initialization.
    diagonal = [sum(row[j] * row[j] for row in rows) for j in range(n)]
    v = _normalize(diagonal)
    if v is None:
        return None
    for _ in range(max(5, int(max_iterations))):
        gv = [0.0] * n
        for row in rows:
            projection = sum(row[j] * v[j] for j in range(n))
            for j in range(n):
                gv[j] += row[j] * projection
        nxt = _normalize(gv)
        if nxt is None:
            return None
        # Eigenvectors have arbitrary sign; align to the previous iterate only
        # for numerical convergence, then canonicalize once at the end.
        if sum(a * b for a, b in zip(nxt, v)) < 0.0:
            nxt = [-x for x in nxt]
        distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(nxt, v)))
        v = nxt
        if distance <= tolerance:
            break

    mean = statistics.fmean(v)
    centered = [x - mean for x in v]
    sd = statistics.stdev(centered) if len(centered) >= 2 else 0.0
    if not math.isfinite(sd) or sd <= 1e-12:
        return None
    factor = [x / sd for x in centered]
    # Canonical sign: first materially non-zero time score is positive.  This is
    # deterministic and still invariant to the input controls' coding signs.
    for value in factor:
        if abs(value) > 1e-12:
            if value < 0.0:
                factor = [-x for x in factor]
            break
    return tuple(factor)
