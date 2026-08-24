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


def labels(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name")) for item in pr.get("labels", []) if item.get("name")}


def select_candidates(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        pr
        for pr in prs
        if str(pr.get("headRefName", "")).startswith("integration/")
        and not bool(pr.get("isDraft"))
        and REQUIRED_LABELS.issubset(labels(pr))
    ]


def render_selection(prs: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    integrations = [pr for pr in prs if str(pr.get("headRefName", "")).startswith("integration/")]
    lines = [
        "# Integration merge selection",
        "",
        f"- open integration PRs: {len(integrations)}",
        f"- administrator-approved candidates: {len(candidates)}",
        "",
        "## Integration queue",
    ]
    if not integrations:
        lines.append("- none")
    else:
        for pr in integrations:
            missing = sorted(REQUIRED_LABELS.difference(labels(pr)))
            lines.append(
                f"- #{pr.get('number')} `{pr.get('headRefName')}` draft={bool(pr.get('isDraft'))} "
                f"merge_state={pr.get('mergeStateStatus', 'UNKNOWN')} missing={','.join(missing) or 'none'}"
            )
    lines.extend(["", "## Decision"])
    if len(candidates) == 0:
        lines.append("No integration is eligible. The incumbent champion remains live.")
    elif len(candidates) == 1:
        lines.append(f"Recheck all gates for PR #{candidates[0].get('number')} before merge.")
    else:
        lines.append(
            "BLOCKED: more than one administrator-approved integration is active. "
            "Only one coherent champion change may be merged per cycle."
        )
    return "\n".join(lines) + "\n"


def validate_candidate(pr: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    current_labels = labels(pr)
    head = str(pr.get("headRefName", ""))
    if not head.startswith("integration/"):
        errors.append("candidate branch is not integration/*")
    if bool(pr.get("isDraft")):
        errors.append("candidate is still draft")
    missing = sorted(REQUIRED_LABELS.difference(current_labels))
    if missing:
        errors.append("candidate is missing labels: " + ", ".join(missing))
    if pr.get("mergeStateStatus") != "CLEAN":
        errors.append(f"merge state is {pr.get('mergeStateStatus')}, not CLEAN")

    checks = pr.get("statusCheckRollup") or []
    names: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name") or check.get("context") or check.get("__typename", "unknown"))
        names.append(name)
        typename = check.get("__typename")
        if typename == "CheckRun":
            if check.get("status") != "COMPLETED":
                errors.append(f"check {name} is not complete")
            elif check.get("conclusion") not in {"SUCCESS", "NEUTRAL"}:
                errors.append(f"check {name} concluded {check.get('conclusion')}")
        else:
            if check.get("state") != "SUCCESS":
                errors.append(f"status {name} is {check.get('state')}")

    for fragment in REQUIRED_CHECK_FRAGMENTS:
        if not any(fragment in name for name in names):
            errors.append(f"required check matching {fragment!r} is missing")

    body = str(pr.get("body") or "")
    normalized = body.lower()
    if "[x] approved research integration into the single champion" not in normalized:
        errors.append("approved integration lifecycle checkbox is not checked")
    if re.search(r"source research pr/branch/commit:\s*(\S+)", body, flags=re.IGNORECASE) is None:
        errors.append("source research PR, branch or commit is missing")
    return errors


def select_main(args: argparse.Namespace) -> int:
    prs = json.loads(Path(args.prs).read_text(encoding="utf-8"))
    if not isinstance(prs, list):
        raise SystemExit("--prs must contain a JSON array")
    candidates = select_candidates(prs)
    Path(args.report).write_text(render_selection(prs, candidates), encoding="utf-8")
    env_lines = [f"CANDIDATE_COUNT={len(candidates)}"]
    if len(candidates) == 1:
        env_lines.append(f"PR_NUMBER={int(candidates[0]['number'])}")
    Path(args.env).write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print(Path(args.report).read_text(encoding="utf-8"), end="")
    return 2 if len(candidates) > 1 else 0


def validate_main(args: argparse.Namespace) -> int:
    pr = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    if not isinstance(pr, dict):
        raise SystemExit("--candidate must contain a JSON object")
    errors = validate_candidate(pr)
    lines = ["# Integration gate", "", f"- PR: #{pr.get('number')}", f"- branch: `{pr.get('headRefName')}`"]
    if errors:
        lines.extend(["", "## Gate errors"])
        lines.extend(f"- {error}" for error in errors)
    else:
        lines.extend(["", "All integration gates passed."])
    report = "\n".join(lines) + "\n"
    Path(args.report).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Select and validate one unified integration PR")
    sub = parser.add_subparsers(dest="command", required=True)

    select_parser = sub.add_parser("select")
    select_parser.add_argument("--prs", required=True)
    select_parser.add_argument("--env", required=True)
    select_parser.add_argument("--report", required=True)
    select_parser.set_defaults(func=select_main)

    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--candidate", required=True)
    validate_parser.add_argument("--report", required=True)
    validate_parser.set_defaults(func=validate_main)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
