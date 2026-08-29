#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RESEARCH_PREFIXES = ("research/", "experiment/", "diagnostic/")


def labels(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name")) for item in pr.get("labels", []) if item.get("name")}


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(value: Any, now: datetime) -> int | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds() // 86400))


def render(prs: list[dict[str, Any]], branches: list[dict[str, Any]], now: datetime) -> str:
    research = [pr for pr in prs if str(pr.get("headRefName", "")).startswith(RESEARCH_PREFIXES)]
    integration = [pr for pr in prs if str(pr.get("headRefName", "")).startswith("integration/")]
    normal = [pr for pr in prs if pr not in research and pr not in integration]
    approved = [pr for pr in research if "research-approved" in labels(pr)]
    shadow = [pr for pr in research if "shadow-isolated" in labels(pr)]

    lines = [
        "# Research and integration queue",
        "",
        f"- generated_at: `{now.isoformat()}`",
        f"- open PRs to main: {len(prs)}",
        f"- research/experiment/diagnostic PRs: {len(research)}",
        f"- approved research awaiting integration: {len(approved)}",
        f"- shadow-isolated research PRs: {len(shadow)}",
        f"- integration PRs: {len(integration)}",
        f"- other focused PRs: {len(normal)}",
        f"- remote branches observed: {len(branches)}",
        "",
        "## Research evidence",
    ]
    if not research:
        lines.append("- none")
    else:
        for pr in research:
            updated_age = age_days(pr.get("updatedAt"), now)
            age_text = "unknown" if updated_age is None else str(updated_age)
            lines.append(
                f"- #{pr.get('number')} `{pr.get('headRefName')}` draft={bool(pr.get('isDraft'))} "
                f"labels={','.join(sorted(labels(pr))) or 'none'} updated_days={age_text} — {pr.get('title')}"
            )

    lines.extend(["", "## Integration backlog"])
    if not integration:
        lines.append("- none")
    else:
        required = {"approved-for-integration", "single-model-reviewed", "administrator-approved"}
        for pr in integration:
            current_labels = labels(pr)
            missing = sorted(required.difference(current_labels))
            lines.append(
                f"- #{pr.get('number')} `{pr.get('headRefName')}` draft={bool(pr.get('isDraft'))} "
                f"merge_state={pr.get('mergeStateStatus', 'UNKNOWN')} "
                f"missing={','.join(missing) or 'none'} — {pr.get('title')}"
            )

    lines.extend(["", "## Focused implementation PRs"])
    if not normal:
        lines.append("- none")
    else:
        for pr in normal:
            lines.append(
                f"- #{pr.get('number')} `{pr.get('headRefName')}` draft={bool(pr.get('isDraft'))} "
                f"labels={','.join(sorted(labels(pr))) or 'none'} — {pr.get('title')}"
            )

    lines.extend(
        [
            "",
            "## Decision boundary",
            "This scheduler inventories evidence only. It cannot approve research, merge code, alter the live champion, "
            "advance `paper-validated`, deploy, or submit orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the read-only Polymarket research queue")
    parser.add_argument("--prs", required=True)
    parser.add_argument("--branches", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    prs = json.loads(Path(args.prs).read_text(encoding="utf-8"))
    branches = json.loads(Path(args.branches).read_text(encoding="utf-8"))
    if not isinstance(prs, list) or not isinstance(branches, list):
        raise SystemExit("PR and branch inputs must be JSON arrays")
    report = render(prs, branches, datetime.now(timezone.utc))
    Path(args.output).write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
