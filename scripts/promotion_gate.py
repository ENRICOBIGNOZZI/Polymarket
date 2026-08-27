#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_promotion_evidence_v1"
POLICY_SCHEMA = "polymarket_automatic_promotion_policy_v1"
RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")
SOURCE_PATTERN = re.compile(r"source research pr/branch/commit:\s*#(\d+)\b", re.I)
CANDIDATE_PATTERN = re.compile(r"promotion candidate:\s*([^\s`]+)", re.I)
EVIDENCE_PATTERN = re.compile(r"promotion evidence file:\s*([^\s`]+\.json)\b", re.I)
OPERATIONAL_RECOVERY_PATTERN = re.compile(
    r"^operational recovery files:\s*([^\n]+)$", re.I | re.M
)
VALIDATED_SOURCE_HEAD_PATTERN = re.compile(
    r"validated source head:\s*`?([0-9a-f]{40})`?", re.I
)
VERDICT_PATTERN = re.compile(
    r"\b(INTEGRATION_READY|APPROVED_FOR_INTEGRATION|MORE_EVIDENCE_REQUIRED|REJECTED)\b",
    re.I,
)
NEGATIVE_VERDICTS = {"MORE_EVIDENCE_REQUIRED", "REJECTED"}
POSITIVE_VERDICTS = {"INTEGRATION_READY", "APPROVED_FOR_INTEGRATION"}
OPERATIONAL_RECOVERY_PATH = re.compile(r"^scripts/paper_v7_execution_loop\.sh$", re.I)
REQUIRED_CANDIDATE_CHECKS = (
    "build-test (Release)",
    "build-test (Debug)",
    "live-paper-smoke",
    "validate",
    "enforce",
)
REQUIRED_SOURCE_CHECKS = (
    "build-test (Release)",
    "build-test (Debug)",
    "enforce",
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def labels(pr: dict[str, Any]) -> set[str]:
    return {
        str(item.get("name"))
        for item in pr.get("labels", [])
        if isinstance(item, dict) and item.get("name")
    }


def marker(body: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(body or "")
    return match.group(1).strip() if match else None


def source_number(candidate: dict[str, Any]) -> int | None:
    value = marker(str(candidate.get("body") or ""), SOURCE_PATTERN)
    return int(value) if value else None


def _check_name(check: dict[str, Any]) -> str:
    return str(check.get("name") or check.get("context") or check.get("__typename", "unknown"))


def _check_timestamp(check: dict[str, Any]) -> str:
    for field in ("completedAt", "updatedAt", "startedAt", "createdAt"):
        value = str(check.get(field) or "").strip()
        if value:
            return value
    return ""


def _check_signature(check: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(check.get("__typename") or ""),
        str(check.get("status") or check.get("state") or ""),
        str(check.get("conclusion") or ""),
    )


def authoritative_check_attempts(
    checks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collapse superseded attempts while staying fail-closed on ambiguity.

    GitHub's ``statusCheckRollup`` can contain multiple attempts for the same
    logical check on one exact head (for example an older CANCELLED run followed
    by a successful rerun). Treating every historical attempt as current makes a
    recovered exact-head check impossible to promote. Prefer the most recent
    timestamped attempt per logical name. If duplicate attempts cannot be ordered
    and disagree, report ambiguity instead of guessing that a success is current.
    """

    grouped: dict[str, list[tuple[int, dict[str, Any], str]]] = {}
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        grouped.setdefault(_check_name(check), []).append((index, check, _check_timestamp(check)))

    selected: list[dict[str, Any]] = []
    ambiguous: list[str] = []
    for name, attempts in grouped.items():
        if len(attempts) == 1:
            selected.append(attempts[0][1])
            continue
        if all(timestamp for _, _, timestamp in attempts):
            _, check, _ = max(attempts, key=lambda item: (item[2], item[0]))
            selected.append(check)
            continue
        signatures = {_check_signature(check) for _, check, _ in attempts}
        if len(signatures) == 1:
            selected.append(attempts[-1][1])
        else:
            ambiguous.append(name)
    return selected, sorted(set(ambiguous))


def check_errors(
    checks: list[dict[str, Any]], required: tuple[str, ...], prefix: str
) -> list[str]:
    errors: list[str] = []
    current_checks, ambiguous = authoritative_check_attempts(checks)
    for name in ambiguous:
        errors.append(f"{prefix}check {name} has ambiguous duplicate attempts")

    names: list[str] = []
    for check in current_checks:
        name = _check_name(check)
        names.append(name)
        if check.get("__typename") == "CheckRun":
            if check.get("status") != "COMPLETED":
                errors.append(f"{prefix}check {name} is not complete")
            elif check.get("conclusion") not in {"SUCCESS", "NEUTRAL"}:
                errors.append(f"{prefix}check {name} concluded {check.get('conclusion')}")
        elif check.get("state") != "SUCCESS":
            errors.append(f"{prefix}status {name} is {check.get('state')}")
    for fragment in required:
        if not any(fragment in name for name in names):
            errors.append(f"{prefix}required check matching {fragment!r} is missing")
    return errors


def is_economic_surface(path: str) -> bool:
    lowered = path.lower()
    if lowered.startswith(("src/", "include/")):
        operational_leafs = ("trade_recorder.cpp", "gamma_client.cpp", "clob_client.cpp")
        return not lowered.endswith(operational_leafs)
    if lowered == "config/live_champion.json" or re.match(r"^config/paper_v\d+\.json$", lowered):
        return True
    if lowered.startswith("config/") and any(
        token in lowered
        for token in ("fast_arb", "cross_venue", "portfolio_supervisor", "allocation", "risk")
    ):
        return True
    if lowered.startswith("scripts/") and any(
        token in lowered
        for token in (
            "paper_v",
            "multi_strategy",
            "intent",
            "hedge",
            "walk_forward",
            "maker",
            "arb",
            "portfolio",
            "allocation",
            "strategy",
            "execution",
            "risk",
            "champion",
        )
    ):
        return True
    return False


def requires_source_content_match(path: str) -> bool:
    return is_economic_surface(path) and path != "config/live_champion.json"


def promotion_class(changed_files: list[str]) -> str:
    return "economic" if any(is_economic_surface(path) for path in changed_files) else "operational"


def declared_operational_recovery_files(candidate: dict[str, Any]) -> list[str]:
    value = marker(str(candidate.get("body") or ""), OPERATIONAL_RECOVERY_PATTERN)
    if not value:
        return []
    return [part.strip().strip("`") for part in value.split(",") if part.strip().strip("`")]


def research_verdict(source: dict[str, Any]) -> str | None:
    events: list[tuple[str, str]] = [("", str(source.get("body") or ""))]
    for comment in source.get("comments") or []:
        if isinstance(comment, dict):
            events.append((str(comment.get("createdAt") or comment.get("created_at") or ""), str(comment.get("body") or "")))
    for review in source.get("reviews") or []:
        if isinstance(review, dict):
            events.append((str(review.get("submittedAt") or review.get("submitted_at") or ""), str(review.get("body") or "")))
    verdicts: list[tuple[str, str]] = []
    for timestamp, text in events:
        for match in VERDICT_PATTERN.finditer(text):
            verdicts.append((timestamp, match.group(1).upper()))
    if not verdicts:
        return None
    verdicts.sort(key=lambda item: item[0])
    return verdicts[-1][1]


def exact_research_governance_verdict(source: dict[str, Any]) -> str | None:
    source_head = str(source.get("headRefOid") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", source_head):
        return None
    events: list[tuple[str, str]] = []
    for comment in source.get("comments") or []:
        if isinstance(comment, dict):
            events.append((str(comment.get("createdAt") or comment.get("created_at") or ""), str(comment.get("body") or "")))
    for review in source.get("reviews") or []:
        if isinstance(review, dict):
            events.append((str(review.get("submittedAt") or review.get("submitted_at") or ""), str(review.get("body") or "")))
    verdicts: list[tuple[str, str]] = []
    for timestamp, text in events:
        if "research governance" not in text.lower():
            continue
        validated = VALIDATED_SOURCE_HEAD_PATTERN.search(text)
        if not validated or validated.group(1).lower() != source_head.lower():
            continue
        for match in VERDICT_PATTERN.finditer(text):
            verdicts.append((timestamp, match.group(1).upper()))
    if not verdicts:
        return None
    verdicts.sort(key=lambda item: item[0])
    return verdicts[-1][1]


def operational_recovery_override(
    candidate: dict[str, Any], source: dict[str, Any], changed_files: list[str]
) -> tuple[bool, list[str], list[str]]:
    declared = declared_operational_recovery_files(candidate)
    if not declared:
        return False, [], []
    errors: list[str] = []
    economic_files = sorted(path for path in changed_files if is_economic_surface(path))
    if not economic_files:
        errors.append("operational_recovery_declaration_without_economic_surface")
    if len(set(declared)) != len(declared):
        errors.append("duplicate_operational_recovery_file")
    for path in declared:
        if not OPERATIONAL_RECOVERY_PATH.fullmatch(path):
            errors.append(f"operational_recovery_path_not_allowlisted:{path}")
    if set(declared) != set(economic_files):
        errors.append("operational_recovery_files_do_not_match_all_economic_files")
    verdict = exact_research_governance_verdict(source)
    if verdict not in POSITIVE_VERDICTS:
        errors.append(
            "operational_recovery_requires_exact_positive_research_governance_verdict"
        )
    return not errors, errors, sorted(set(declared))


def validate_windows(windows: list[dict[str, Any]], minimum: int, require_non_overlap: bool) -> list[str]:
    errors: list[str] = []
    if len(windows) < minimum:
        errors.append(f"insufficient_independent_test_windows:{len(windows)}<{minimum}")
        return errors
    parsed: list[tuple[int, int]] = []
    for row in windows:
        start = integer(row.get("start_ts"))
        end = integer(row.get("end_ts"))
        if start <= 0 or end <= start:
            errors.append("invalid_test_window")
            continue
        parsed.append((start, end))
    if require_non_overlap and len(parsed) == len(windows):
        parsed.sort()
        for (_, previous_end), (next_start, _) in zip(parsed, parsed[1:]):
            if next_start < previous_end:
                errors.append("overlapping_test_windows")
                break
    return errors


def validate_evidence(evidence: dict[str, Any], candidate: dict[str, Any], source: dict[str, Any], alpha_config: dict[str, Any], policy: dict[str, Any], now: int) -> list[str]:
    errors: list[str] = []
    if evidence.get("schema") != SCHEMA:
        errors.append("unexpected_promotion_evidence_schema")
    if evidence.get("paper_only") is not True:
        errors.append("promotion_evidence_not_paper_only")
    if evidence.get("authenticated_execution") is not False:
        errors.append("promotion_evidence_authenticated_execution_must_be_false")
    if evidence.get("real_order_submission") is not False:
        errors.append("promotion_evidence_real_order_submission_must_be_false")
    if str(evidence.get("decision") or "").lower() != "integration_ready":
        errors.append("promotion_evidence_decision_not_integration_ready")

    candidate_body = str(candidate.get("body") or "")
    source_body = str(source.get("body") or "")
    candidate_id = marker(candidate_body, CANDIDATE_PATTERN)
    source_candidate_id = marker(source_body, CANDIDATE_PATTERN)
    evidence_candidate_id = str(evidence.get("candidate_id") or "")
    if not candidate_id:
        errors.append("integration_missing_promotion_candidate_marker")
    if not source_candidate_id:
        errors.append("source_missing_promotion_candidate_marker")
    if candidate_id and source_candidate_id and candidate_id != source_candidate_id:
        errors.append("promotion_candidate_marker_mismatch")
    if candidate_id and evidence_candidate_id != candidate_id:
        errors.append("promotion_evidence_candidate_id_mismatch")

    candidate_path = marker(candidate_body, EVIDENCE_PATTERN)
    source_path = marker(source_body, EVIDENCE_PATTERN)
    prefix = str(policy.get("evidence_path_prefix") or "research/promotion_evidence/")
    if not candidate_path or not source_path:
        errors.append("promotion_evidence_file_marker_missing")
    elif candidate_path != source_path:
        errors.append("promotion_evidence_file_marker_mismatch")
    elif not candidate_path.startswith(prefix) or ".." in Path(candidate_path).parts:
        errors.append("promotion_evidence_file_outside_allowed_prefix")

    if policy.get("require_source_head_binding", True):
        source_sha = str(source.get("headRefOid") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            errors.append("source_head_sha_missing_or_invalid")
        elif str(evidence.get("source_head_sha") or "") != source_sha:
            errors.append("promotion_evidence_source_head_sha_mismatch")

    generated = integer(evidence.get("generated_ts"))
    max_age = integer(policy.get("max_evidence_age_seconds"), 21600)
    if generated <= 0:
        errors.append("promotion_evidence_generated_ts_missing")
    elif now < generated - 60:
        errors.append("promotion_evidence_timestamp_in_future")
    elif now - generated > max_age:
        errors.append("promotion_evidence_stale")

    if policy.get("block_negative_research_verdicts", True):
        verdict = research_verdict(source)
        if verdict in NEGATIVE_VERDICTS:
            errors.append(f"latest_research_verdict_blocks_promotion:{verdict}")

    evidence_ids = [str(item) for item in evidence.get("evidence_ids") or [] if str(item)]
    gates = alpha_config.get("gates") or {}
    min_passes = integer(gates.get("min_consecutive_passes"), 3)
    if len(evidence_ids) < min_passes:
        errors.append(f"insufficient_independent_evidence_ids:{len(evidence_ids)}<{min_passes}")
    if len(set(evidence_ids)) != len(evidence_ids):
        errors.append("duplicate_evidence_ids")
    windows = evidence.get("test_windows") or []
    if not isinstance(windows, list):
        windows = []
    errors.extend(validate_windows(windows, min_passes, bool(policy.get("require_non_overlapping_test_windows", True))))

    metrics = evidence.get("metrics") or {}
    min_trades = integer(gates.get("min_oos_trades"), 30)
    max_drawdown = finite(gates.get("max_drawdown"), 0.10)
    min_profit_factor = finite(gates.get("min_profit_factor"), 1.10)
    max_pvalue = finite(gates.get("max_bootstrap_pvalue"), 0.10)
    fdr_q = finite(gates.get("fdr_q"), 0.10)
    min_folds = integer(gates.get("min_active_folds"), 2)
    min_positive_fraction = finite(gates.get("min_positive_fold_fraction"), 0.50)
    min_incremental = finite(gates.get("min_incremental_utility"), 0.0)
    if integer(metrics.get("oos_trades")) < min_trades:
        errors.append("oos_trade_gate")
    if finite(metrics.get("oos_net_pnl_usd")) <= 0.0:
        errors.append("oos_net_pnl_gate")
    if finite(metrics.get("stressed_1_5x_net_pnl_usd")) <= 0.0:
        errors.append("cost_stress_1_5x_gate")
    if finite(metrics.get("stressed_2_0x_net_pnl_usd")) <= 0.0:
        errors.append("cost_stress_2_0x_gate")
    if finite(metrics.get("max_drawdown"), math.inf) > max_drawdown:
        errors.append("drawdown_gate")
    if finite(metrics.get("profit_factor")) < min_profit_factor:
        errors.append("profit_factor_gate")
    if finite(metrics.get("bootstrap_one_sided_pvalue"), 1.0) > max_pvalue:
        errors.append("bootstrap_gate")
    if finite(metrics.get("fdr_adjusted_pvalue"), 1.0) > fdr_q:
        errors.append("fdr_gate")
    if integer(metrics.get("active_folds")) < min_folds:
        errors.append("active_folds_gate")
    if finite(metrics.get("positive_fold_fraction")) < min_positive_fraction:
        errors.append("fold_stability_gate")
    if finite(metrics.get("incremental_utility"), float("-inf")) <= min_incremental:
        errors.append("incremental_utility_gate")
    if metrics.get("single_model_compatible") is not True:
        errors.append("single_model_compatibility_gate")
    if str(metrics.get("data_health") or "").lower() != "healthy":
        errors.append("data_health_gate")
    return errors


def evaluate(candidate: dict[str, Any], source: dict[str, Any], changed_files: list[str], evidence: dict[str, Any] | None, alpha_config: dict[str, Any], policy: dict[str, Any], now: int, require_approval_label: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    if policy.get("schema") != POLICY_SCHEMA:
        errors.append("unexpected_promotion_policy_schema")
    if policy.get("paper_only") is not True:
        errors.append("promotion_policy_must_be_paper_only")
    if policy.get("manual_approval_required") is not False:
        errors.append("manual_approval_must_be_disabled")

    if not str(candidate.get("headRefName") or "").startswith("integration/"):
        errors.append("candidate_branch_not_integration")
    if candidate.get("isDraft") is True:
        errors.append("candidate_is_draft")
    if candidate.get("mergeStateStatus") != "CLEAN":
        errors.append("candidate_merge_state_not_clean")
    errors.extend(check_errors(candidate.get("statusCheckRollup") or [], REQUIRED_CANDIDATE_CHECKS, "candidate "))

    expected_source = source_number(candidate)
    if expected_source is None:
        errors.append("numbered_source_research_pr_missing")
    elif integer(source.get("number"), -1) != expected_source:
        errors.append("source_research_pr_number_mismatch")
    if not str(source.get("headRefName") or "").startswith(RESEARCH_PREFIXES):
        errors.append("source_branch_not_research")
    errors.extend(check_errors(source.get("statusCheckRollup") or [], REQUIRED_SOURCE_CHECKS, "source "))

    approval_label = str(policy.get("approval_label") or "autonomous-promotion-approved")
    if require_approval_label and approval_label not in labels(candidate):
        errors.append("autonomous_promotion_label_missing")

    kind = promotion_class(changed_files)
    recovery_files: list[str] = []
    if kind == "economic":
        recovery_ok, recovery_errors, recovery_files = operational_recovery_override(
            candidate, source, changed_files
        )
        if recovery_ok:
            kind = "operational"
        elif recovery_files or declared_operational_recovery_files(candidate):
            errors.extend(recovery_errors)

    if kind == "economic":
        if evidence is None:
            errors.append("economic_promotion_requires_machine_readable_evidence")
        else:
            errors.extend(validate_evidence(evidence, candidate, source, alpha_config, policy, now))

    metrics = (evidence or {}).get("metrics") or {}
    score = finite(metrics.get("incremental_utility"), 0.0) if kind == "economic" else 0.0
    return {
        "schema": "polymarket_automatic_promotion_gate_v1",
        "eligible": not errors,
        "promotion_class": kind,
        "score": score,
        "candidate_pr": integer(candidate.get("number")),
        "source_pr": integer(source.get("number")),
        "candidate_id": marker(str(candidate.get("body") or ""), CANDIDATE_PATTERN),
        "evidence_path": marker(str(candidate.get("body") or ""), EVIDENCE_PATTERN),
        "economic_files": sorted(path for path in changed_files if is_economic_surface(path)),
        "operational_recovery_files": recovery_files,
        "source_content_match_files": sorted(path for path in changed_files if requires_source_content_match(path)),
        "errors": sorted(set(errors)),
        "manual_approval_required": False,
        "authenticated_execution": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed automatic paper-promotion gate")
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--alpha-config", type=Path, default=Path("config/alpha_factory.json"))
    parser.add_argument("--policy", type=Path, default=Path("config/promotion_policy.json"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--now", type=int, default=None)
    parser.add_argument("--require-approval-label", action="store_true")
    args = parser.parse_args()

    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    source = json.loads(args.source.read_text(encoding="utf-8"))
    changed_files = [line.strip() for line in args.changed_files.read_text(encoding="utf-8").splitlines() if line.strip()]
    evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence and args.evidence.exists() else None
    alpha_config = json.loads(args.alpha_config.read_text(encoding="utf-8"))
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    result = evaluate(candidate, source, changed_files, evidence, alpha_config, policy, int(time.time()) if args.now is None else args.now, require_approval_label=args.require_approval_label)

    lines = [
        "# Automatic paper-promotion decision", "",
        f"- eligible: `{str(result['eligible']).lower()}`",
        f"- class: `{result['promotion_class']}`",
        f"- candidate PR: `#{result['candidate_pr']}`",
        f"- source research PR: `#{result['source_pr']}`",
        f"- candidate id: `{result.get('candidate_id') or 'n/a'}`",
        f"- evidence path: `{result.get('evidence_path') or 'n/a'}`",
        f"- economic files: `{len(result.get('economic_files') or [])}`",
        f"- approved operational recovery files: `{len(result.get('operational_recovery_files') or [])}`",
        "- manual approval required: `false`",
        "- authenticated real-money execution: `false`",
    ]
    if result["errors"]:
        lines.extend(["", "## Blocking reasons"])
        lines.extend(f"- {item}" for item in result["errors"])
    else:
        lines.extend(["", "All applicable objective gates passed. The controller may authorize paper promotion."])
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.json_output:
        args.json_output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.report.read_text(encoding="utf-8"), end="")
    return 0 if result["eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
