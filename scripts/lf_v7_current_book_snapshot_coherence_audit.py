#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def two_control_factor(control_a: float, control_b: float) -> float:
    """PC1 score for two equally loaded standardized controls."""
    return (float(control_a) + float(control_b)) / math.sqrt(2.0)


def residual(target: float, control_a: float, control_b: float, beta: float = 1.0) -> float:
    return float(target) - float(beta) * two_control_factor(control_a, control_b)


def deterministic_counterexample() -> dict[str, float | bool]:
    """Show how an asynchronous control snapshot creates a false residual.

    The training factor has two equally oriented controls, so PC1 is
    (c1+c2)/sqrt(2). At a true synchronous current state c1=c2=+2 and target
    exposure beta=1, target=2*sqrt(2) has residual zero. If c1 is instead a
    stale -2 snapshot while c2 and the target are current, the mixed-time factor
    is zero and the apparent residual is +2*sqrt(2), despite no true residual
    dislocation at the decision timestamp.
    """
    target_current = 2.0 * math.sqrt(2.0)
    synchronous_c1 = 2.0
    synchronous_c2 = 2.0
    stale_c1 = -2.0
    current_c2 = 2.0
    synchronous_factor = two_control_factor(synchronous_c1, synchronous_c2)
    mixed_factor = two_control_factor(stale_c1, current_c2)
    synchronous_residual = residual(target_current, synchronous_c1, synchronous_c2)
    mixed_residual = residual(target_current, stale_c1, current_c2)
    return {
        "target_current": target_current,
        "synchronous_factor": synchronous_factor,
        "mixed_time_factor": mixed_factor,
        "true_synchronous_residual": synchronous_residual,
        "mixed_time_apparent_residual": mixed_residual,
        "mixed_time_abs_residual": abs(mixed_residual),
        "crosses_lf_abs_z_floor_if_residual_sd_is_one": abs(mixed_residual) >= 0.75,
        "crosses_pca_abs_z_floor_if_residual_sd_is_one": abs(mixed_residual) >= 1.0,
    }


def source_contract(repo_root: Path) -> dict[str, Any]:
    lf_data_path = repo_root / "scripts" / "v7_local_factor_data.py"
    pca_driver_path = repo_root / "scripts" / "v7_pca_stat_arb_research.py"
    lf_data = lf_data_path.read_text(encoding="utf-8")
    pca_driver = pca_driver_path.read_text(encoding="utf-8")
    return {
        "lf_book_reads_exchange_timestamp": 'row.get("timestamp")' in lf_data,
        "lf_book_reads_snapshot_hash": 'row.get("hash")' in lf_data,
        "pca_fetches_books_in_bulk": "token_books = base.fetch_books" in pca_driver,
        "pca_assigns_post_fetch_global_receive_clock": "books_received_ts = int(time.time())" in pca_driver,
        "pca_reuses_global_receive_clock_for_every_market": "received_ts=books_received_ts" in pca_driver,
    }


def build_report(repo_root: Path) -> dict[str, Any]:
    contract = source_contract(repo_root)
    counterexample = deterministic_counterexample()
    blocked = (
        not contract["lf_book_reads_exchange_timestamp"]
        and not contract["lf_book_reads_snapshot_hash"]
        and contract["pca_assigns_post_fetch_global_receive_clock"]
        and contract["pca_reuses_global_receive_clock_for_every_market"]
    )
    return {
        "schema_version": 1,
        "research_only": True,
        "paper_only": True,
        "authenticated_execution": False,
        "finding": "cross_market_current_book_snapshot_coherence_not_identified",
        "structural_blocker": blocked,
        "source_contract": contract,
        "deterministic_counterexample": counterexample,
        "interpretation": (
            "A cross-sectional residual is only a current-state statistic when target and all nuisance controls "
            "refer to a causally coherent book snapshot. Per-book exchange/receive clocks and snapshot identity "
            "are therefore required before Local Factor or PCA current-state scoring can be promotion evidence."
        ),
        "required_successor": [
            "preserve CLOB per-book exchange timestamp and snapshot/hash identity",
            "record a local receive timestamp for each book response or book item",
            "require target and every predeclared control to pass bounded age and cross-sectional skew gates",
            "fail closed on missing clock or snapshot provenance",
            "score the frozen LF PC1/PCA basis only after the coherent current cross-section is established",
            "keep training/bootstrap/multiplicity and PAPER safety contracts unchanged",
        ],
        "decision": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V7 LF/PCA current-book cross-sectional snapshot coherence")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(args.repo_root.resolve())
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
