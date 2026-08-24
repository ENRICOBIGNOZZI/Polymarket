#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

# Paper promotion is automatic once objective checks pass. Legacy approval labels
# may still exist on old PRs, but they are not gates.
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


def select_candidates(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        pr
        for pr in prs
        if str(pr.get("headRefName", "")).startswith("integration/")
        and not bool(pr.get("isDraft"))
        and source_research_pr_number(pr) is not None
    ]
    return sorted(candidates, key=lambda pr: int(pr.get("number") or 0))


def render_selection(prs: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    integrations = [pr for pr in prs if str(pr.get("headRefName", "")).startswith("integration/")]
    selected = candidates[0] if candidates else None
    lines = [
        "# Automatic paper-promotion selection",
        "",
        f"- open integration PRs: {len(integrations)}",
        f"- automation-eligible integration PRs: {len(candidates)}",
        f"- selected this cycle: #{selected.get('number')}" if selected else "- selected this cycle: none",
        "",
        "## Integration queue",
    ]
    if not integrations:
        lines.append("- none")
    else:
        eligible_numbers = {int(pr.get("number") or 0) for pr in candidates}
        for pr in sorted(integrations, key=lambda item: int(item.get("number") or 0)):
            source = source_research_pr_number(pr)
            number = int(pr.get("number") or 0)
            lines.append(
                f"- #{number} `{pr.get('headRefName')}` draft={bool(pr.get('isDraft'))} "
                f"merge_state={pr.get('mergeStateStatus', 'UNKNOWN')} "
                f"source_research_pr={source if source is not None else 'missing'} "
                f"automation_candidate={'yes' if number in eligible_numbers else 'no'}"
            )
    lines.extend(["", "## Decision"])
    if selected is None:
        lines.append("No paper champion integration is eligible in this cycle.")
    elif len(candidates) == 1:
        lines.append(f"Recheck objective candidate/source gates and automatically merge PR #{selected.get('number')}.")
    else:
        lines.append(
            f"Multiple candidates are eligible; promote one deterministically this cycle, starting with PR #{selected.get('number')}. "
            "Remaining candidates stay queued for later cycles rather than blocking the control plane."
        )
    return "\n".join(lines) + "\n"


def validate_source_research(candidate: dict[str, Any], source: dict[str, Any] | None) -> list[str]:
    errors: list[str] = []
    expected_number = source_research_pr_number(candidate)
    if expected_number is None:
        return [
            "source research must be a numbered PR using "
            "`Source research PR/branch/commit: #<number>`"
        ]
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
    errors.extend(
        _check_errors(
            source.get("statusCheckRollup") or [],
            SOURCE_REQUIRED_CHECK_FRAGMENTS,
            prefix="source research ",
        )
    )
    return errors


def validate_candidate(pr: dict[str, Any], source_research: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    head = str(pr.get("headRefName", ""))
    if not head.startswith("integration/"):
        errors.append("candidate branch is not integration/*")
    if bool(pr.get("isDraft")):
        errors.append("candidate is still draft")
    if pr.get("mergeStateStatus") != "CLEAN":
        errors.append(f"merge state is {pr.get('mergeStateStatus')}, not CLEAN")

    errors.extend(_check_errors(pr.get("statusCheckRollup") or [], REQUIRED_CHECK_FRAGMENTS))

    source_number = source_research_pr_number(pr)
    if source_number is None:
        errors.extend(validate_source_research(pr, source_research))
    elif source_research is not None:
        errors.extend(validate_source_research(pr, source_research))
    return errors


def select_main(args: argparse.Namespace) -> int:
    prs = json.loads(Path(args.prs).read_text(encoding="utf-8"))
    if not isinstance(prs, list):
        raise SystemExit("--prs must contain a JSON array")
    candidates = select_candidates(prs)
    selected = candidates[0] if candidates else None
    Path(args.report).write_text(render_selection(prs, candidates), encoding="utf-8")
    env_lines = [
        f"ELIGIBLE_COUNT={len(candidates)}",
        f"CANDIDATE_COUNT={1 if selected is not None else 0}",
    ]
    if selected is not None:
        env_lines.append(f"PR_NUMBER={int(selected['number'])}")
        env_lines.append(f"SOURCE_PR_NUMBER={source_research_pr_number(selected)}")
    Path(args.env).write_text("\n".join(env_lines) + "\n", encoding="utf-8")
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
        "# Automatic paper-promotion gate",
        "",
        f"- PR: #{pr.get('number')}",
        f"- branch: `{pr.get('headRefName')}`",
        f"- source research PR: `{source_research_pr_number(pr) or 'missing'}`",
        "- manual approval labels required: `false`",
    ]
    if errors:
        lines.extend(["", "## Gate errors"])
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.extend(["", "All objective paper-promotion gates passed; the merge scheduler may promote automatically."])
    report = "\n".join(lines) + "\n"
    Path(args.report).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Select and validate automatic paper champion integrations")
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
