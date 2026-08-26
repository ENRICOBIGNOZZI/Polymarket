#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FlowCoverage:
    discovered_conditions: int
    recent_trade_conditions: int
    overlap_conditions: int
    overlap_fraction: float
    classification: str
    execution_evidence_eligible: bool


def _clean(values: Iterable[str]) -> set[str]:
    return {str(value).strip() for value in values if str(value).strip()}


def classify_flow_coverage(
    discovered_conditions: Iterable[str],
    recent_trade_conditions: Iterable[str],
    *,
    min_overlap_fraction: float = 0.01,
) -> FlowCoverage:
    """Classify whether a nominally discovered universe overlaps causal recent flow.

    A globally active tape with zero overlap is not evidence of zero fill probability;
    it is evidence that the selected execution universe is mismatched to observed flow.
    The audit is intentionally fail-closed and does not infer a fill probability.
    """
    if not 0.0 <= min_overlap_fraction <= 1.0:
        raise ValueError("min_overlap_fraction must be in [0,1]")

    discovered = _clean(discovered_conditions)
    recent = _clean(recent_trade_conditions)
    overlap = discovered & recent
    fraction = len(overlap) / len(discovered) if discovered else 0.0

    if not discovered:
        classification = "NO_DISCOVERED_UNIVERSE"
        eligible = False
    elif not recent:
        classification = "NO_RECENT_GLOBAL_FLOW"
        eligible = False
    elif not overlap:
        classification = "ACTIVE_UNIVERSE_MISMATCH"
        eligible = False
    elif fraction < min_overlap_fraction:
        classification = "INSUFFICIENT_ACTIVE_FLOW_COVERAGE"
        eligible = False
    else:
        classification = "ACTIVE_FLOW_COVERAGE_PRESENT"
        eligible = True

    return FlowCoverage(
        discovered_conditions=len(discovered),
        recent_trade_conditions=len(recent),
        overlap_conditions=len(overlap),
        overlap_fraction=fraction,
        classification=classification,
        execution_evidence_eligible=eligible,
    )


def audit_diagnostic(payload: dict, *, min_overlap_fraction: float = 0.01) -> dict:
    """Audit the bounded trade-API diagnostic emitted by the V6 live smoke.

    The diagnostic currently reports counts rather than every condition id.  When it
    explicitly classifies a live global tape with zero sampled-universe matches, that
    state is sufficient to fail closed without manufacturing condition identities.
    """
    probes = payload.get("probes") or {}
    global_recent = probes.get("global_recent") or {}
    global_rows = int(global_recent.get("local_window_rows") or 0)
    global_matches = int(global_recent.get("local_window_discovered_matches") or 0)
    discovered = int(payload.get("discovered_conditions") or 0)

    if discovered <= 0:
        classification = "NO_DISCOVERED_UNIVERSE"
        eligible = False
    elif global_rows <= 0:
        classification = "NO_RECENT_GLOBAL_FLOW"
        eligible = False
    elif global_matches <= 0:
        classification = "ACTIVE_UNIVERSE_MISMATCH"
        eligible = False
    else:
        fraction = global_matches / discovered
        classification = (
            "ACTIVE_FLOW_COVERAGE_PRESENT"
            if fraction >= min_overlap_fraction
            else "INSUFFICIENT_ACTIVE_FLOW_COVERAGE"
        )
        eligible = classification == "ACTIVE_FLOW_COVERAGE_PRESENT"

    return {
        "schema": "lf_v6_active_flow_universe_audit_v1",
        "classification": classification,
        "execution_evidence_eligible": eligible,
        "discovered_conditions": discovered,
        "recent_global_rows": global_rows,
        "recent_discovered_matches": global_matches,
        "min_overlap_fraction": min_overlap_fraction,
        "source_classification": payload.get("classification", ""),
        "paper_only": True,
        "authenticated_execution": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V6 LF/Graph active-flow universe coverage")
    parser.add_argument("diagnostic", type=Path, help="trade_api_empty_tape_diagnostic_v1 JSON")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-overlap-fraction", type=float, default=0.01)
    args = parser.parse_args()

    payload = json.loads(args.diagnostic.read_text(encoding="utf-8"))
    result = audit_diagnostic(payload, min_overlap_fraction=args.min_overlap_fraction)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["execution_evidence_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
