#!/usr/bin/env python3
"""Fail-closed synthetic V7 internal latency guardrail."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = load(args.config)
    result = load(args.benchmark)
    if config.get("schema") != "polymarket_v7_latency_slo_v1":
        raise ValueError("unexpected latency SLO schema")
    if result.get("paper_only") is not True:
        raise ValueError("benchmark did not attest PAPER-only")
    if result.get("representative_venue_replay") is not False:
        raise ValueError("synthetic CI gate cannot claim representative venue replay")
    if result.get("includes_network_or_clob") is not False:
        raise ValueError("synthetic CI gate cannot include or claim network/CLOB evidence")

    stages = result.get("stages_ns")
    limits = config.get("ci_internal_guardrails_ns")
    if not isinstance(stages, dict) or not isinstance(limits, dict):
        raise ValueError("missing stage measurements or guardrails")

    checks: list[dict[str, Any]] = []
    passed = True
    for stage, percentiles in limits.items():
        if not isinstance(percentiles, dict) or not isinstance(stages.get(stage), dict):
            raise ValueError(f"missing stage {stage}")
        for percentile, ceiling in percentiles.items():
            observed = stages[stage].get(percentile)
            if not isinstance(observed, int) or not isinstance(ceiling, int):
                raise ValueError(f"invalid {stage}.{percentile}")
            ok = observed <= ceiling
            passed = passed and ok
            checks.append({"stage": stage, "percentile": percentile,
                           "observed_ns": observed, "ceiling_ns": ceiling, "passed": ok})

    verdict = {
        "schema": "polymarket_v7_latency_gate_result_v1",
        "passed": passed,
        "scope": "synthetic_internal_compute_only",
        "representative_venue_replay": False,
        "network_or_clob_proven": False,
        "competitive_percentile_proven": False,
        "checks": checks,
    }
    rendered = json.dumps(verdict, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
