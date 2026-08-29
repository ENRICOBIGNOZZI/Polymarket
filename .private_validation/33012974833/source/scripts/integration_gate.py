#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_LABELS = {
    "approved-for-integration",
    "single-model-reviewed",
    "administrator-approved",
}
REQUIRED_CHECK_FRAGMENTS = (
    "build-test (Release)",
    "build-test (Debug)",
    "live-paper-smoke",
    "validate",
    "enforce",
)
SOURCE_REQUIRED_CHECK_FRAGMENTS = (
    "build-test (Release)",
    "build-test (Debug)",
    "enforce",
)
RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")
SOURCE_RESEARCH_PR_PATTERN = re.compile(
    r"source research pr/branch/commit:\s*#(\d+)\b", flags=re.IGNORECASE
)


def labels(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name")) for item in pr.get("labels", []) if item.get("name")}


def source_research_pr_number(pr: dict[str, Any]) -> int | None:
    match = SOURCE_RESEARCH_PR_PATTERN.search(str(pr.get("body") or ""))
    return int(match.group(1)) if match else None


def _check_errors(
    checks: list[dict[str, Any]], required_fragments: tuple[str, ...], *, prefix: str = ""
) -> list[str]:
    errors: list[str] = []
    names: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name") or check.get("context") or check.get("__typename", "unknown"))
        names.append(name)
        typename = check.get("__typename")
        if typename == "CheckRun":
            if check.get("status") != "COMPLETED":
                errors.append(f"{prefix}check {name} is not complete")
            elif check.get("conclusion") not in {"SUCCESS", "NEUTRAL"}:
                errors.append(f"{prefix}check {name} concluded {check.get('conclusion')}")
        else:
            if check.get("state") != "SUCCESS":
                errors.append(f"{prefix}status {name} is {check.get('state')}")
    for fragment in required_fragments:
        if not any(fragment in name for name in names):
            errors.append(f"{prefix}required check matching {fragment!r} is missing")
    return errors


def candidate_local_errors(pr: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(pr.get("headRefName", "")).startswith("integration/"):
        errors.append("candidate branch is not integration/*")
    if bool(pr.get("isDraft")):
        errors.append("candidate is still draft")
    missing = sorted(REQUIRED_LABELS.difference(labels(pr)))
    if missing:
        errors.append("candidate is missing labels: " + ", ".join(missing))
    if source_research_pr_number(pr) is None:
        errors.append("candidate has no numbered source research PR")
    if "[x] approved research integration into the single champion" not in str(pr.get("body") or "").lower():
        errors.append("approved integration lifecycle checkbox is not checked")
    if pr.get("mergeStateStatus") != "CLEAN":
        errors.append(f"merge state is {pr.get('mergeStateStatus')}, not CLEAN")
    errors.extend(_check_errors(pr.get("statusCheckRollup") or [], REQUIRED_CHECK_FRAGMENTS))
    return errors


def select_candidates(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [pr for pr in prs if not candidate_local_errors(pr)]
    return sorted(candidates, key=lambda pr: int(pr.get("number") or 0))


def render_selection(prs: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    integrations = [pr for pr in prs if str(pr.get("headRefName", "")).startswith("integration/")]
    ready_numbers = {int(pr.get("number") or 0) for pr in candidates}
    lines = [
        "# Approval-gated paper integration queue",
        "",
        f"- open integration PRs: {len(integrations)}",
        f"- fully approved candidates queued for source validation: {len(candidates)}",
        "- source validation policy: research-approved provenance plus green source checks",
        "",
        "## Integration queue",
    ]
    if not integrations:
        lines.append("- none")
    for pr in sorted(integrations, key=lambda item: int(item.get("number") or 0)):
        number = int(pr.get("number") or 0)
        errors = candidate_local_errors(pr)
        reason = "; ".join(errors) if errors else "candidate approval and local gates green"
        lines.append(
            f"- #{number} `{pr.get('headRefName')}` source=#{source_research_pr_number(pr) or 'missing'} "
            f"queue={'yes' if number in ready_numbers else 'no'} — {reason}"
        )
    lines.extend(["", "## Decision"])
    if candidates:
        lines.append(
            "Probe approved candidates in deterministic PR-number order and merge at most one whose numbered "
            "source research PR is research-approved and green."
        )
    else:
        lines.append("No approved integration is ready. The incumbent champion remains authoritative.")
    return "\n".join(lines) + "\n"


def validate_source_research(candidate: dict[str, Any], source: dict[str, Any] | None) -> list[str]:
    errors: list[str] = []
    expected_number = source_research_pr_number(candidate)
    if expected_number is None:
        return ["source research must be a numbered PR using `Source research PR/branch/commit: #<number>`"]
    if source is None:
        return ["source research metadata was not supplied"]
    try:
        actual_number = int(source.get("number"))
    except (TypeError, ValueError):
        actual_number = -1
    if actual_number != expected_number:
        errors.append(f"source research PR is #{actual_number}, expected #{expected_number}")
    source_head = str(source.get("headRefName", ""))
    if not source_head.startswith(RESEARCH_PREFIXES):
        errors.append("source research branch is not research/*, experiment/*, or diagnostic/*")
    source_labels = labels(source)
    if "research-approved" not in source_labels:
        errors.append("source research PR is not research-approved")
    misplaced = sorted(source_labels.intersection(REQUIRED_LABELS))
    if misplaced:
        errors.append("source research PR carries integration labels: " + ", ".join(misplaced))
    errors.extend(
        _check_errors(
            source.get("statusCheckRollup") or [],
            SOURCE_REQUIRED_CHECK_FRAGMENTS,
            prefix="source research ",
        )
    )
    return errors


def validate_candidate(pr: dict[str, Any], source_research: dict[str, Any] | None = None) -> list[str]:
    errors = candidate_local_errors(pr)
    if source_research is not None or source_research_pr_number(pr) is None:
        errors.extend(validate_source_research(pr, source_research))
    return errors


def select_main(args: argparse.Namespace) -> int:
    prs = json.loads(Path(args.prs).read_text(encoding="utf-8"))
    if not isinstance(prs, list):
        raise SystemExit("--prs must contain a JSON array")
    candidates = select_candidates(prs)
    Path(args.report).write_text(render_selection(prs, candidates), encoding="utf-8")
    queue = ",".join(str(int(item["number"])) for item in candidates)
    Path(args.env).write_text(
        f"ELIGIBLE_COUNT={len(candidates)}\nQUEUE_NUMBERS={queue}\nCANDIDATE_COUNT=0\n",
        encoding="utf-8",
    )
    print(Path(args.report).read_text(encoding="utf-8"), end="")
    return 0


def validate_main(args: argparse.Namespace) -> int:
    pr = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    if not isinstance(pr, dict):
        raise SystemExit("--candidate must contain a JSON object")
    source = None
    if args.source_research:
        source = json.loads(Path(args.source_research).read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise SystemExit("--source-research must contain a JSON object")
    errors = validate_candidate(pr, source)
    lines = [
        "# Approval-gated paper integration",
        "",
        f"- PR: #{pr.get('number')}",
        f"- branch: `{pr.get('headRefName')}`",
        f"- source research PR: `{source_research_pr_number(pr) or 'missing'}`",
        "- research approval required: `true`",
        "- administrator approval required: `true`",
    ]
    if errors:
        lines.extend(["", "## Gate errors"])
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.extend(["", "All research, administrator, CI and live-paper integration gates passed."])
    report = "\n".join(lines) + "\n"
    Path(args.report).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Select and validate approval-gated paper champion integrations")
    sub = parser.add_subparsers(dest="command", required=True)
    select_parser = sub.add_parser("select")
    select_parser.add_argument("--prs", required=True)
    select_parser.add_argument("--env", required=True)
    select_parser.add_argument("--report", required=True)
    select_parser.set_defaults(func=select_main)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--candidate", required=True)
    validate_parser.add_argument("--source-research")
    validate_parser.add_argument("--report", required=True)
    validate_parser.set_defaults(func=validate_main)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
