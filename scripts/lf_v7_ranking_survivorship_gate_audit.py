#!/usr/bin/env python3
"""Audit the V7 frozen-ranking workflow for survivorship-safe evidence gating.

Research-only: this module never dispatches workflows, writes canonical refs, or
changes strategy/risk state. It only inspects workflow text supplied by callers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict


UNSAFE_ASSERTION = 'assert report["survivorship_safe"] is False'
SAFE_ASSERTION = 'assert report["survivorship_safe"] is True'
REPORT_METRIC_MARKER = "mean_daily_rank_ic"


@dataclass(frozen=True)
class RankingSurvivorshipAudit:
    workflow_has_unsafe_survivorship_assertion: bool
    workflow_requires_survivorship_safe_true: bool
    workflow_publishes_ranking_metrics: bool
    promotion_evidence_contract_valid: bool
    decision: str


def audit_workflow_text(text: str) -> RankingSurvivorshipAudit:
    unsafe = UNSAFE_ASSERTION in text
    safe = SAFE_ASSERTION in text
    publishes = REPORT_METRIC_MARKER in text
    valid = safe and not unsafe
    return RankingSurvivorshipAudit(
        workflow_has_unsafe_survivorship_assertion=unsafe,
        workflow_requires_survivorship_safe_true=safe,
        workflow_publishes_ranking_metrics=publishes,
        promotion_evidence_contract_valid=valid,
        decision="PASS" if valid else "BLOCKING_SURVIVORSHIP_EVIDENCE_CONTRACT",
    )


def audit_workflow(path: Path) -> Dict[str, object]:
    return asdict(audit_workflow_text(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "workflow",
        nargs="?",
        default=".github/workflows/v7-cross-sectional-ranking-research.yml",
    )
    args = parser.parse_args()
    print(json.dumps(audit_workflow(Path(args.workflow)), indent=2, sort_keys=True))
