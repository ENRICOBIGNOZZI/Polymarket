#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Iterable


def sample_mean_sd(values: Iterable[float]) -> tuple[float, float]:
    xs = list(values)
    if len(xs) < 2:
        raise ValueError("each series needs at least two observations")
    mean = sum(xs) / len(xs)
    variance = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    sd = math.sqrt(max(0.0, variance))
    if not math.isfinite(sd) or sd <= 1e-12:
        raise ValueError("each series must have nonzero finite variance")
    return mean, sd


def standardized_series(
    series: dict[str, dict[int, float]],
) -> tuple[list[str], dict[str, dict[int, float]]]:
    names = sorted(series)
    if len(names) < 2:
        raise ValueError("at least two series are required")
    out: dict[str, dict[int, float]] = {}
    for name in names:
        mean, sd = sample_mean_sd(series[name].values())
        out[name] = {ts: (value - mean) / sd for ts, value in series[name].items()}
    return names, out


def pairwise_overlap_matrix(
    series: dict[str, dict[int, float]], min_common: int = 24
) -> tuple[list[str], list[list[float]], list[list[int]]]:
    names, z = standardized_series(series)
    n = len(names)
    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    counts = [[0 for _ in range(n)] for _ in range(n)]
    for i in range(n):
        matrix[i][i] = 1.0
        counts[i][i] = len(z[names[i]])
        for j in range(i + 1, n):
            common = set(z[names[i]]) & set(z[names[j]])
            counts[i][j] = counts[j][i] = len(common)
            if len(common) < min_common:
                continue
            value = sum(z[names[i]][ts] * z[names[j]][ts] for ts in common) / len(common)
            value = max(-1.0, min(1.0, value))
            matrix[i][j] = matrix[j][i] = value
    return names, matrix, counts


def masked_gram_matrix(
    series: dict[str, dict[int, float]],
) -> tuple[list[str], list[list[float]], list[list[int]]]:
    names, z = standardized_series(series)
    n = len(names)
    counts = [[0 for _ in range(n)] for _ in range(n)]
    gram = [[0.0 for _ in range(n)] for _ in range(n)]
    norms = [0.0 for _ in range(n)]

    for i, name in enumerate(names):
        norms[i] = sum(value * value for value in z[name].values())
        counts[i][i] = len(z[name])
        gram[i][i] = norms[i]

    for i in range(n):
        for j in range(i + 1, n):
            common = set(z[names[i]]) & set(z[names[j]])
            counts[i][j] = counts[j][i] = len(common)
            gram_ij = sum(z[names[i]][ts] * z[names[j]][ts] for ts in common)
            gram[i][j] = gram[j][i] = gram_ij

    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    for i in range(n):
        matrix[i][i] = 1.0
        for j in range(i + 1, n):
            denom = math.sqrt(norms[i] * norms[j])
            value = gram[i][j] / denom if denom > 1e-12 else 0.0
            value = max(-1.0, min(1.0, value))
            matrix[i][j] = matrix[j][i] = value
    return names, matrix, counts


def symmetric_eigenvalues(matrix: list[list[float]], tol: float = 1e-12) -> list[float]:
    n = len(matrix)
    if n == 0 or any(len(row) != n for row in matrix):
        raise ValueError("matrix must be nonempty and square")
    a = [row[:] for row in matrix]
    max_iter = max(64, 50 * n * n)
    for _ in range(max_iter):
        p, q, off = 0, 0, 0.0
        for i in range(n):
            for j in range(i + 1, n):
                candidate = abs(a[i][j])
                if candidate > off:
                    p, q, off = i, j, candidate
        if off <= tol:
            break
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        tau = (aqq - app) / (2.0 * apq)
        t = math.copysign(1.0 / (abs(tau) + math.sqrt(1.0 + tau * tau)), tau)
        c = 1.0 / math.sqrt(1.0 + t * t)
        s = t * c
        for k in range(n):
            if k in (p, q):
                continue
            akp, akq = a[k][p], a[k][q]
            a[k][p] = a[p][k] = c * akp - s * akq
            a[k][q] = a[q][k] = s * akp + c * akq
        a[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
        a[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
        a[p][q] = a[q][p] = 0.0
    return sorted(a[i][i] for i in range(n))


def load_long_csv(path: Path) -> dict[str, dict[int, float]]:
    series: dict[str, dict[int, float]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "series", "value"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("input CSV must contain timestamp,series,value")
        for row in reader:
            name = row["series"].strip()
            if not name:
                continue
            ts = int(row["timestamp"])
            value = float(row["value"])
            if not math.isfinite(value):
                continue
            series.setdefault(name, {})[ts] = value
    return series


def analyze(series: dict[str, dict[int, float]], min_common: int = 24) -> dict[str, object]:
    names, pairwise, counts = pairwise_overlap_matrix(series, min_common=min_common)
    _, masked, _ = masked_gram_matrix(series)
    pairwise_eigs = symmetric_eigenvalues(pairwise)
    masked_eigs = symmetric_eigenvalues(masked)
    tolerance = 1e-9
    offdiag_counts = [
        counts[i][j]
        for i in range(len(names))
        for j in range(i + 1, len(names))
        if counts[i][j] > 0
    ]
    return {
        "schema": "polymarket_lf_sparse_factor_diagnostic_v1",
        "series": len(names),
        "series_names": names,
        "min_common": min_common,
        "pairwise_overlap": {
            "min_eigenvalue": min(pairwise_eigs),
            "negative_eigenvalues": sum(value < -tolerance for value in pairwise_eigs),
            "eigenvalues": pairwise_eigs,
        },
        "masked_gram": {
            "min_eigenvalue": min(masked_eigs),
            "negative_eigenvalues": sum(value < -tolerance for value in masked_eigs),
            "eigenvalues": masked_eigs,
        },
        "overlap": {
            "min_pair_observations": min(offdiag_counts) if offdiag_counts else 0,
            "max_pair_observations": max(offdiag_counts) if offdiag_counts else 0,
        },
        "pairwise_psd_defect": min(pairwise_eigs) < -tolerance,
        "masked_gram_psd": min(masked_eigs) >= -tolerance,
        "production_change": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Research-only diagnostic for sparse-panel factor covariance integrity"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-common", type=int, default=24)
    args = parser.parse_args()
    if args.min_common < 2:
        raise SystemExit("--min-common must be at least 2")
    report = analyze(load_long_csv(args.input), min_common=args.min_common)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
