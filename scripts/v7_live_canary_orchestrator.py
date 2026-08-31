#!/usr/bin/env python3
"""Validate V7 canary preconditions without signing, connecting, or trading."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import v7_security_audit as security


STAGES = ("AUTHENTICATED_READ_ONLY", "BALANCE_ALLOWANCE_DRY_RUN", "POST_ONLY_PLACE_CANCEL", "RESTING_MAKER_PROBE", "CONTROLLED_FAK_PROBE", "PARTIAL_FILL_CANCEL", "SETTLEMENT_LIFECYCLE", "RECONCILE", "ATTEST")


def plan(root: Path, *, stage: str) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError("unknown_canary_stage")
    paper = json.loads((root / "config/paper_v7.json").read_text(encoding="utf-8"))
    caps = json.loads((root / "config/v7_live_caps_zero.json").read_text(encoding="utf-8"))
    audit = security.audit(root)
    blocked = paper.get("paper_only") is not True or caps.get("live_enabled") is not False or audit["authenticated_execution_allowed"] is not True
    return {"schema_version": 1, "stage": stage, "execution_performed": False,
            "state": "PRE_CANARY_BLOCKED" if blocked else "EXTERNAL_LIVE_CAPABILITY_REQUIRED",
            "reason_codes": audit["reason_codes"] if blocked else ["nonzero_external_live_limits_required"],
            "next_stage": STAGES[STAGES.index(stage) + 1] if stage != STAGES[-1] else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--stage", default=STAGES[0], choices=STAGES)
    args = parser.parse_args()
    print(json.dumps(plan(args.repository_root.resolve(), stage=args.stage), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
