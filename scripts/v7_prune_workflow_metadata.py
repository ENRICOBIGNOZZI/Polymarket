#!/usr/bin/env python3
"""Disable active GitHub Actions workflows whose files no longer exist on main."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def tracked_workflow_paths(root: Path) -> set[str]:
    workflow_root = root / ".github" / "workflows"
    return {
        path.relative_to(root).as_posix()
        for pattern in ("*.yml", "*.yaml")
        for path in workflow_root.glob(pattern)
        if path.is_file()
    }


def stale_active_workflows(
    workflows: list[dict[str, Any]], tracked: set[str],
) -> list[dict[str, Any]]:
    return sorted(
        (
            workflow
            for workflow in workflows
            if workflow.get("state") == "active"
            and str(workflow.get("path") or "").startswith(".github/workflows/")
            and str(workflow.get("path")) not in tracked
        ),
        key=lambda workflow: (str(workflow.get("path")), int(workflow.get("id", 0))),
    )


def request_json(
    url: str, token: str | None, method: str = "GET",
) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "polymarket-v7-workflow-hygiene",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"GitHub workflow request failed: {method} {url}: {error}") from error
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GitHub workflow request returned invalid JSON: {url}") from error


def fetch_workflows(repository: str, token: str | None) -> list[dict[str, Any]]:
    workflows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = request_json(
            f"https://api.github.com/repos/{repository}/actions/workflows"
            f"?per_page=100&page={page}",
            token,
        )
        page_items = payload.get("workflows") if isinstance(payload, dict) else None
        if not isinstance(page_items, list):
            raise RuntimeError("GitHub workflow inventory returned an invalid response")
        workflows.extend(page_items)
        if len(page_items) < 100:
            return workflows
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--github-repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.github_repository:
        raise RuntimeError("--github-repository is required")

    root = args.repository_root.resolve()
    token = os.environ.get("GITHUB_TOKEN")
    workflows = fetch_workflows(args.github_repository, token)
    tracked = tracked_workflow_paths(root)
    stale = stale_active_workflows(workflows, tracked)
    disabled: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if args.apply:
        for workflow in stale:
            workflow_id = int(workflow["id"])
            try:
                request_json(
                    f"https://api.github.com/repos/{args.github_repository}/actions/workflows/"
                    f"{workflow_id}/disable",
                    token,
                    method="PUT",
                )
                disabled.append({"id": workflow_id, "path": workflow["path"]})
            except RuntimeError as error:
                failures.append({"id": workflow_id, "path": workflow["path"], "error": str(error)})

    report = {
        "schema": "polymarket_v7_workflow_metadata_cleanup_v1",
        "apply": args.apply,
        "workflow_record_count": len(workflows),
        "tracked_workflow_count": len(tracked),
        "stale_active_count": len(stale),
        "stale_active": [{"id": int(item["id"]), "path": item["path"]} for item in stale],
        "disabled_count": len(disabled),
        "disabled": disabled,
        "failures": failures,
    }
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
