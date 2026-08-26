#!/usr/bin/env python3
"""Fail-closed economic promotion gate for V7 Fast Structural Arbitrage.

Quoted/depth-walk structural opportunity evidence is only a research pre-screen.
Promotion requires same-SHA canonical-ledger joint execution evidence with verified
post-cost realized fill-conditioned PAPER PnL after cost stress. This module is
read-only with respect to runtime/champion/canonical refs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

GATE_SCHEMA = "polymarket_v7_fast_promotion_gate_v1"
EXECUTION_SCHEMA = "polymarket_v7_fast_joint_execution_v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def execution_reasons(
    execution: dict[str, Any],
    *,
    expected_sha: str,
    min_realized_pnl_observations: int,
    min_complete_baskets: int,
) -> list[str]:
    reasons: list[str] = []
    if not execution:
        return ["canonical_joint_execution_evidence_missing"]
    if execution.get("schema") != EXECUTION_SCHEMA:
        reasons.append("invalid_joint_execution_schema")
    if execution.get("source") != "canonical_v7_execution_ledger":
        reasons.append("noncanonical_execution_source")
    if execution.get("model_sha") != expected_sha:
        reasons.append("mixed_or_wrong_sha_execution_evidence")
    if execution.get("paper_only") is not True:
        reasons.append("paper_boundary_missing")
    if execution.get("authenticated_execution") is not False:
        reasons.append("authenticated_execution_boundary_missing")
    if execution.get("point_in_time") is not True:
        reasons.append("point_in_time_contract_missing")
    if execution.get("authoritative_fees") is not True:
        reasons.append("authoritative_fee_contract_missing")
    if execution.get("depth_executable") is not True:
        reasons.append("executable_depth_contract_missing")
    if execution.get("partial_unwind_accounted") is not True:
        reasons.append("partial_unwind_contract_missing")
    if execution.get("post_cost_pnl_verified") is not True:
        reasons.append("post_cost_pnl_contract_missing")
    if integer(execution.get("joint_state_observations")) < min_realized_pnl_observations:
        reasons.append("insufficient_joint_state_observations")
    if integer(execution.get("realized_pnl_observations")) < min_realized_pnl_observations:
        reasons.append("insufficient_realized_pnl_observations")
    if integer(execution.get("completed_baskets")) < min_complete_baskets:
        reasons.append("insufficient_completed_baskets")
    if number(execution.get("fill_conditioned_net_pnl")) <= 0.0:
        reasons.append("nonpositive_fill_conditioned_net_pnl")
    if number(execution.get("cost_stress_1_5x_net_pnl")) <= 0.0:
        reasons.append("nonpositive_1_5x_cost_stress_pnl")
    if number(execution.get("cost_stress_2x_net_pnl")) <= 0.0:
        reasons.append("nonpositive_2x_cost_stress_pnl")
    return reasons


def gate_candidate(
    candidate: dict[str, Any],
    execution: dict[str, Any],
    *,
    expected_sha: str,
    min_realized_pnl_observations: int = 20,
    min_complete_baskets: int = 20,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(expected_sha):
        raise ValueError("expected_sha must be an exact 40-character lowercase Git SHA")
    if candidate.get("real_order_submission") is not False:
        raise ValueError("candidate must explicitly disable real order submission")
    policy = candidate.get("candidate_policy")
    if not isinstance(policy, dict) or policy.get("real_order_submission") is not False:
        raise ValueError("candidate policy must explicitly disable real order submission")
    quoted_theory_ready = bool(candidate.get("promotion_ready"))
    reasons = execution_reasons(
        execution,
        expected_sha=expected_sha,
        min_realized_pnl_observations=min_realized_pnl_observations,
        min_complete_baskets=min_complete_baskets,
    )
    if candidate.get("mode") != "research_only":
        reasons.append("candidate_not_research_only")
    execution_ready = not reasons
    promotion_ready = quoted_theory_ready and execution_ready
    output = dict(candidate)
    output["schema_version"] = max(2, integer(candidate.get("schema_version"), 1))
    output["model_sha"] = expected_sha
    output["quoted_theory_promotion_ready"] = quoted_theory_ready
    output["promotion_ready"] = promotion_ready
    output["promotion_gate"] = {
        "schema": GATE_SCHEMA,
        "requires_same_sha": True,
        "requires_canonical_ledger": True,
        "requires_joint_execution_states": True,
        "requires_realized_fill_conditioned_pnl": True,
        "requires_post_cost_pnl_contract": True,
        "requires_cost_stress_1_5x_and_2x": True,
        "execution_ready": execution_ready,
        "reasons": reasons,
    }
    safe_policy = dict(policy)
    safe_policy["model_sha"] = expected_sha
    safe_policy["quoted_theory_promotion_ready"] = bool(
        policy.get("promotion_ready", quoted_theory_ready)
    )
    safe_policy["promotion_ready"] = promotion_ready
    safe_policy["real_order_submission"] = False
    output["candidate_policy"] = safe_policy
    return output


def rewrite_header(path: Path, *, promotion_ready: bool, expected_sha: str) -> None:
    text = path.read_text(encoding="utf-8")
    replacement = "true" if promotion_ready else "false"
    text, count = re.subn(
        r"inline constexpr bool kPromotionReady = (?:true|false);",
        f"inline constexpr bool kPromotionReady = {replacement};",
        text,
        count=1,
    )
    if count != 1:
        raise ValueError("generated header is missing kPromotionReady")
    marker = "// V7 promotion gate: canonical-ledger realized joint execution evidence required.\n"
    if marker not in text:
        text = text.replace("#pragma once\n", "#pragma once\n\n" + marker, 1)
    sha_decl = f'inline constexpr char kPromotionModelSha[] = "{expected_sha}";\n'
    if "kPromotionModelSha" not in text:
        text = text.replace(
            "} // namespace pm::fast::generated",
            sha_decl + "} // namespace pm::fast::generated",
        )
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--execution-evidence", type=Path)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--generated-header", type=Path)
    parser.add_argument("--min-realized-pnl-observations", type=int, default=20)
    parser.add_argument("--min-complete-baskets", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidate = read_json(args.candidate)
    if not candidate:
        raise SystemExit("candidate JSON is missing or invalid")
    execution = read_json(args.execution_evidence)
    gated = gate_candidate(
        candidate,
        execution,
        expected_sha=args.expected_sha,
        min_realized_pnl_observations=max(1, args.min_realized_pnl_observations),
        min_complete_baskets=max(1, args.min_complete_baskets),
    )
    atomic_json(args.output_json or args.candidate, gated)
    if args.generated_header is not None:
        rewrite_header(
            args.generated_header,
            promotion_ready=bool(gated["promotion_ready"]),
            expected_sha=args.expected_sha,
        )
    print(
        json.dumps(
            {
                "model_sha": args.expected_sha,
                "quoted_theory_promotion_ready": bool(
                    gated.get("quoted_theory_promotion_ready")
                ),
                "promotion_ready": bool(gated.get("promotion_ready")),
                "reasons": gated["promotion_gate"]["reasons"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
