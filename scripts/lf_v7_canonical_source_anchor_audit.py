#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

LF_REQUIRED = (
    "scripts/v7_local_factor_core.py",
    "scripts/v7_local_factor_inference.py",
    "config/research_v7_local_factor.json",
)

PCA_REQUIRED = (
    "scripts/v7_pca_stat_arb_core.py",
    "scripts/v7_pca_stat_arb_inference.py",
    "scripts/v7_pca_stat_arb_research.py",
    "config/research_v7_pca_stat_arb.json",
)

RANKING_REQUIRED = (
    "scripts/v7_cross_sectional_rank.py",
    "scripts/v7_cross_sectional_rank_core.py",
    "scripts/v7_cross_sectional_rank_fast.py",
    "scripts/v7_cross_sectional_rank_inference.py",
    "scripts/v7_cross_sectional_history.py",
    "scripts/v7_cross_sectional_relative.py",
    "scripts/v7_cross_sectional_tail_relative.py",
    "config/research_v7_cross_sectional_rank_frozen.json",
    "tests/test_v7_cross_sectional_rank.py",
    "tests/test_v7_cross_sectional_rank_fast.py",
    "tests/test_v7_cross_sectional_rank_inference.py",
    "tests/test_v7_cross_sectional_history.py",
    "tests/test_v7_cross_sectional_relative.py",
)

RANKING_WORKFLOW = ".github/workflows/v7-cross-sectional-ranking-research.yml"
DEFERRED_MARKER = "deferred until V7 ranking implementation is present on this revision"


def _path_status(root: Path, required: Iterable[str]) -> dict[str, object]:
    required_list = list(required)
    present = [p for p in required_list if (root / p).is_file()]
    missing = [p for p in required_list if not (root / p).is_file()]
    return {
        "required": required_list,
        "present": present,
        "missing": missing,
        "ready": not missing,
    }


def audit_repo(root: Path) -> dict[str, object]:
    root = root.resolve()
    workflow_path = root / RANKING_WORKFLOW
    workflow_present = workflow_path.is_file()
    workflow_text = workflow_path.read_text(encoding="utf-8") if workflow_present else ""
    ranking_deferred_contract = DEFERRED_MARKER in workflow_text

    lf = _path_status(root, LF_REQUIRED)
    pca = _path_status(root, PCA_REQUIRED)
    ranking = _path_status(root, RANKING_REQUIRED)

    model_source_ready = bool(lf["ready"] and pca["ready"] and ranking["ready"])
    decision = "CANONICAL_V7_MODEL_SOURCE_READY" if model_source_ready else "CANONICAL_V7_MODEL_SOURCE_MISSING"

    return {
        "schema_version": 1,
        "root": str(root),
        "families": {
            "local_factor": lf,
            "pca": pca,
            "cross_sectional_ranking": ranking,
        },
        "ranking_workflow": {
            "path": RANKING_WORKFLOW,
            "present": workflow_present,
            "deferred_until_implementation_lands": ranking_deferred_contract,
        },
        "canonical_model_source_ready": model_source_ready,
        "decision": decision,
        "research_state": "READY_FOR_EXACT_SHA_EVIDENCE" if model_source_ready else "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether canonical V7 LF/PCA/ranking research primitives exist on this exact repository revision.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--output", default="", help="Optional JSON output path")
    args = parser.parse_args()

    report = audit_repo(Path(args.root))
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
