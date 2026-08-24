#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClusterSpec:
    name: str
    size: int
    rho: float

    @property
    def leading_eigenvalue(self) -> float:
        return 1.0 + (self.size - 1) * self.rho

    @property
    def local_explained_share(self) -> float:
        return self.leading_eigenvalue / float(self.size)


def allocate_global_factor_budget(clusters: list[ClusterSpec], factors: int) -> list[str]:
    if factors < 0:
        raise ValueError("factors must be non-negative")
    ranked = sorted(clusters, key=lambda c: (-c.leading_eigenvalue, c.name))
    return [cluster.name for cluster in ranked[:factors]]


def inspect_incumbent_source(text: str) -> dict[str, object]:
    global_marker = "auto vecs = top_eigenvectors(C, factors, eigenvalues);"
    relation_marker = "pm::market_relation(*s[j].market, *s[i].market)"
    global_pos = text.find(global_marker)
    relation_pos = text.find(relation_marker)
    return {
        "global_factor_basis_detected": global_pos >= 0,
        "relation_restricted_hedges_detected": relation_pos >= 0,
        "global_basis_precedes_relation_filter": global_pos >= 0 and relation_pos >= 0 and global_pos < relation_pos,
        "fixed_default_factor_budget_detected": "factors = 3" in text,
    }


def deterministic_fixture() -> list[ClusterSpec]:
    # Four independent semantic/event clusters, each internally dominated by one
    # strong common factor. With a global factor budget K=3, one economically
    # coherent cluster must be omitted even though its own local factor explains
    # most of the within-cluster variance.
    return [
        ClusterSpec("cluster_a", 4, 0.98),
        ClusterSpec("cluster_b", 4, 0.94),
        ClusterSpec("cluster_c", 4, 0.90),
        ClusterSpec("cluster_d", 4, 0.86),
    ]


def build_report(source_text: str, factors: int = 3) -> dict[str, object]:
    clusters = deterministic_fixture()
    retained = allocate_global_factor_budget(clusters, factors)
    rows = []
    for cluster in clusters:
        rows.append(
            {
                **asdict(cluster),
                "leading_eigenvalue": cluster.leading_eigenvalue,
                "local_explained_share": cluster.local_explained_share,
                "retained_by_global_budget": cluster.name in retained,
                "global_common_factor_coverage": 1.0 if cluster.name in retained else 0.0,
            }
        )
    omitted = [row for row in rows if not row["retained_by_global_budget"]]
    return {
        "schema": "polymarket_lf_relation_aligned_factor_diagnostic_v1",
        "incumbent": inspect_incumbent_source(source_text),
        "fixture": {
            "global_factor_budget": factors,
            "cluster_count": len(clusters),
            "retained_clusters": retained,
            "clusters": rows,
            "omitted_cluster_count": len(omitted),
            "omitted_locally_strong_clusters": [
                row["name"] for row in omitted if row["local_explained_share"] >= 0.80
            ],
        },
        "interpretation": {
            "finding": (
                "A fixed global PCA basis can spend its limited factor budget on unrelated clusters "
                "before the later relation filter restricts hedge legs. Relation-local factors can "
                "therefore be absent even when they explain most within-cluster variation."
            ),
            "candidate": (
                "Evaluate a hierarchical or relation-aligned factor basis on the same chronological "
                "history: global factors only where economically hedgeable, plus cluster-local factors "
                "for event/semantic relative value."
            ),
            "evidence_state": "MORE_EVIDENCE_REQUIRED",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose global-vs-relation-aligned B2 factor capacity")
    parser.add_argument("--source", default="src/pca_stat_arb.cpp")
    parser.add_argument("--factors", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args()

    source_text = Path(args.source).read_text(encoding="utf-8")
    report = build_report(source_text, factors=args.factors)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    print(payload, end="")

    incumbent = report["incumbent"]
    fixture = report["fixture"]
    if not all(incumbent.values()):
        return 2
    if not fixture["omitted_locally_strong_clusters"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
