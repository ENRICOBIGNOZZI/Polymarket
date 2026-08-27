#!/usr/bin/env python3
"""Audit V7 multi-leg execution evidence at the economic bundle unit.

The canonical execution-evidence sidecar is intentionally left unchanged by
this research audit.  The audit shows whether leg-level entry fills divided by
bundle-level submissions can be interpreted as a joint-completion rate.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "scripts" / "v7_execution_evidence.py"
BASE_SHA = "b9e19f5702d9452fb6921ada012404a60d3b47b0"


def load_sidecar() -> Any:
    spec = importlib.util.spec_from_file_location("v7_execution_evidence_multileg_audit", SIDECAR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SIDECAR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def submissions(bundle_count: int, required_legs: int) -> list[dict[str, str]]:
    return [
        {
            "action": "SUBMIT",
            "intent_id": f"bundle-{bundle}",
            "bundle_id": f"bundle-{bundle}",
            "required_legs": str(required_legs),
            "expected_edge": "0.01",
        }
        for bundle in range(bundle_count)
    ]


def leg_fills(bundle_count: int, leg_indexes: tuple[int, ...]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    timestamp = 1_700_000_000
    for bundle in range(bundle_count):
        for leg in leg_indexes:
            rows.append(
                {
                    "timestamp": str(timestamp),
                    "action": "BUY",
                    "bundle_id": f"bundle-{bundle}",
                    "leg_id": f"leg-{leg}",
                    "fill_id": f"fill-{bundle}-{leg}",
                    "order_id": f"order-{bundle}-{leg}",
                }
            )
            timestamp += 1
    return rows


def economic_completion_rate(
    submission_rows: list[dict[str, str]], execution_rows: list[dict[str, str]]
) -> tuple[int, float]:
    required: dict[str, int] = {}
    for row in submission_rows:
        bundle = str(row.get("bundle_id") or row.get("intent_id") or "")
        if not bundle:
            continue
        required[bundle] = max(1, int(row.get("required_legs") or 1))

    filled_legs: dict[str, set[str]] = defaultdict(set)
    for row in execution_rows:
        bundle = str(row.get("bundle_id") or "")
        leg = str(row.get("leg_id") or "")
        if bundle and leg:
            filled_legs[bundle].add(leg)

    complete = sum(len(filled_legs[bundle]) >= needed for bundle, needed in required.items())
    rate = complete / len(required) if required else 0.0
    return complete, rate


def incumbent_metrics(sidecar: Any, submission_rows: list[dict[str, str]], execution_rows: list[dict[str, str]]) -> dict[str, Any]:
    fills, raw_fills, unidentified_fills = sidecar.unique_evidence_rows(
        execution_rows, sidecar.row_is_fill, sidecar.fill_identity
    )
    submitted, raw_submissions, unidentified_submissions = sidecar.unique_evidence_rows(
        submission_rows, sidecar.row_is_submission, sidecar.submission_identity
    )
    fill_rate = len(fills) / len(submitted) if submitted else None
    complete, completion_rate = economic_completion_rate(submission_rows, execution_rows)
    return {
        "unique_leg_entry_fills": len(fills),
        "unique_bundle_submissions": len(submitted),
        "incumbent_fill_rate": fill_rate,
        "economic_joint_completions": complete,
        "economic_joint_completion_rate": completion_rate,
        "raw_fill_rows": raw_fills,
        "unidentified_fill_rows": unidentified_fills,
        "raw_submission_rows": raw_submissions,
        "unidentified_submission_rows": unidentified_submissions,
    }


def audit() -> dict[str, Any]:
    sidecar = load_sidecar()

    # Every submitted two-leg bundle fills exactly one leg.  The current
    # leg-fill / bundle-submission ratio is 100%, yet no economic bundle ever
    # jointly completes.  It also satisfies the default min_fills=20 count.
    partial_submissions = submissions(bundle_count=20, required_legs=2)
    partial_fills = leg_fills(bundle_count=20, leg_indexes=(0,))
    partial = incumbent_metrics(sidecar, partial_submissions, partial_fills)
    partial.update(
        {
            "default_min_fills_satisfied": partial["unique_leg_entry_fills"] >= 20,
            "one_percent_fill_gate_satisfied": bool(
                partial["incumbent_fill_rate"] is not None and partial["incumbent_fill_rate"] >= 0.01
            ),
            "joint_completion_gate_should_fail": partial["economic_joint_completions"] == 0,
        }
    )

    # A complete five-leg bundle generates five entry-fill records for one
    # economic submission.  Therefore the current ratio is not probability-like
    # and can exceed one even in the benign fully-completed case.
    complete_submissions = submissions(bundle_count=4, required_legs=5)
    complete_fills = leg_fills(bundle_count=4, leg_indexes=(0, 1, 2, 3, 4))
    complete = incumbent_metrics(sidecar, complete_submissions, complete_fills)

    relative_execution_paths, relative_submission_paths = sidecar.strategy_paths(Path("RUN"), "relative_value")

    return {
        "schema": "lf_v7_multileg_completion_evidence_audit_v1",
        "base_main_sha": BASE_SHA,
        "source": "scripts/v7_execution_evidence.py",
        "source_contract": {
            "relative_value_execution_paths": [str(path) for path in relative_execution_paths],
            "relative_value_submission_paths": [str(path) for path in relative_submission_paths],
            "current_metric": "unique leg entry fills / unique submissions",
            "required_economic_metric": "jointly completed bundles / submitted bundles",
        },
        "counterexamples": {
            "twenty_two_leg_bundles_one_leg_filled_each": partial,
            "four_five_leg_bundles_all_legs_filled": complete,
        },
        "material_finding": (
            "For multi-leg relative-value evidence, leg-level entry fills and bundle-level submissions are different "
            "economic units. The current ratio can equal 1.0 with zero joint completions and can exceed 1.0. "
            "Therefore min_fills/min_fill_rate do not establish executable multi-leg completion evidence."
        ),
        "required_successor_contract": [
            "Preserve unique leg fills as execution events, but aggregate promotion evidence at one economic bundle/opportunity unit.",
            "Persist the required leg set, target quantities and candidate/bundle identity before entry.",
            "Build causal bundle states including none, each partial-leg subset, joint completion and terminal unwind/settlement.",
            "Define completion_rate as economically completed bundles divided by submitted bundles so it is bounded in [0,1].",
            "Fail closed when required-leg or target-size provenance is absent; do not infer completion from a count of leg fill rows.",
            "Link terminal PnL to the same bundle state and include partial abort/unwind, authoritative fees, slippage, adverse markout, capital and latency costs.",
            "Stress the same frozen completed/partial bundle observations at 1x/1.5x/2x without reselecting states.",
        ],
        "decision": "MORE_EVIDENCE_REQUIRED",
        "production_mutated": False,
        "paper_only": True,
        "authenticated_execution": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = audit()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
