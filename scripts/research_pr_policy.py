#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")
INTEGRATION_LABELS = {
    "approved-for-integration",
    "single-model-reviewed",
    "administrator-approved",
}


def label_names(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name")) for item in pr.get("labels", []) if item.get("name")}


def evaluate(
    event: dict[str, Any], changed_files: set[str], manifest_existed_on_base: bool
) -> tuple[list[str], dict[str, Any]]:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        return ["event does not contain pull_request metadata"], {}

    head = str(pr.get("head", {}).get("ref") or pr.get("headRefName") or "")
    body = str(pr.get("body") or "")
    draft = bool(pr.get("draft"))
    labels = label_names(pr)
    manifest_changed = "config/live_champion.json" in changed_files
    errors: list[str] = []

    if head.startswith(RESEARCH_PREFIXES):
        forbidden = sorted(labels.intersection(INTEGRATION_LABELS))
        if forbidden:
            errors.append(
                "research/experiment/diagnostic PRs cannot carry integration or administrator labels: "
                + ", ".join(forbidden)
            )
        if not draft and "shadow-isolated" not in labels:
            errors.append(
                "unapproved research PRs must remain draft/close-without-merge; "
                "only tested shadow-isolated instrumentation may be non-draft"
            )
        if manifest_changed:
            errors.append("research and diagnostic branches may never change the live champion manifest")
    elif head.startswith("integration/"):
        if not draft:
            missing = sorted(INTEGRATION_LABELS.difference(labels))
            if missing:
                errors.append("non-draft integration PR is missing labels: " + ", ".join(missing))
            normalized = body.lower()
            if "[x] approved research integration into the single champion" not in normalized:
                errors.append("integration PR must check the approved-research lifecycle box")
            source = re.search(
                r"source research pr/branch/commit:\s*(\S+)", body, flags=re.IGNORECASE
            )
            if source is None:
                errors.append("integration PR must link a source research PR, branch or commit")
    else:
        misplaced = sorted(labels.intersection(INTEGRATION_LABELS | {"research-approved"}))
        if misplaced:
            errors.append(
                "research/integration approval labels are valid only on their dedicated branch classes: "
                + ", ".join(misplaced)
            )
        if manifest_changed and manifest_existed_on_base:
            errors.append("an existing live champion manifest may change only on integration/*")

    summary = {
        "branch": head,
        "draft": draft,
        "labels": sorted(labels),
        "manifest_changed": manifest_changed,
        "changed_files": len(changed_files),
        "policy": "pass" if not errors else "fail",
    }
    return errors, summary


def render(summary: dict[str, Any], errors: list[str]) -> str:
    lines = ["# Research pull-request policy", ""]
    for key in ("branch", "draft", "labels", "manifest_changed", "changed_files", "policy"):
        value = summary.get(key, "unknown")
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "none"
        lines.append(f"- {key}: `{value}`")
    if errors:
        lines.extend(["", "## Policy errors"])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Enforce Polymarket research/integration PR policy")
    parser.add_argument("--event", required=True)
    parser.add_argument("--changed-files", required=True)
    parser.add_argument("--manifest-existed-on-base", choices=("true", "false"), required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    changed = {
        line.strip()
        for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    errors, summary = evaluate(
        event,
        changed,
        manifest_existed_on_base=args.manifest_existed_on_base == "true",
    )
    report = render(summary, errors)
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
