#!/usr/bin/env python3
"""Validate a one-shot GitHub-push approval for the V7 PAPER deploy.

This is an alternative trigger transport for the existing deploy workflow.
It does not relax PAPER-only, cutover, exact-SHA, Tailscale, SSH, runtime,
single-writer, or health gates.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from typing import Any

SCHEMA = "polymarket_v7_paper_deploy_request_v1"
REQUEST_PATH = "deploy/v7-paper-deploy-request.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_KEYS = {
    "schema",
    "version",
    "request_id",
    "expected_parent_sha",
    "cutover_approved",
    "paper_only",
    "authenticated_execution",
    "real_order_submission",
    "freeze_maker_forward_window",
    "trigger",
}


class DeployRequestError(ValueError):
    pass


def _exact_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise DeployRequestError(name)
    return value


def load_request(path: Path, *, event_before: str, event_after: str) -> dict[str, Any]:
    event_before = _exact_sha(event_before, "event_before")
    event_after = _exact_sha(event_after, "event_after")
    if event_before == event_after:
        raise DeployRequestError("push_did_not_advance")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise DeployRequestError("unsafe_request_file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeployRequestError("invalid_request_json") from exc
    if not isinstance(value, dict) or set(value) != REQUIRED_KEYS:
        raise DeployRequestError("request_fields")
    if value.get("schema") != SCHEMA or value.get("version") != 1:
        raise DeployRequestError("request_schema")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_id):
        raise DeployRequestError("request_id")
    if _exact_sha(value.get("expected_parent_sha"), "expected_parent_sha") != event_before:
        raise DeployRequestError("request_parent_mismatch")
    if value.get("cutover_approved") is not True:
        raise DeployRequestError("cutover_not_approved")
    if value.get("paper_only") is not True:
        raise DeployRequestError("paper_only_required")
    if value.get("authenticated_execution") is not False:
        raise DeployRequestError("authenticated_execution_must_remain_false")
    if value.get("real_order_submission") is not False:
        raise DeployRequestError("real_order_submission_must_remain_false")
    if value.get("freeze_maker_forward_window") is not False:
        raise DeployRequestError("push_trigger_does_not_freeze_forward_window")
    if value.get("trigger") != "GITHUB_PUSH_ONE_SHOT":
        raise DeployRequestError("trigger_contract")
    return value


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise DeployRequestError("git_validation_failed:" + exc.output.strip()) from exc


def validate_commit(root: Path, *, event_before: str, event_after: str,
                    request_path: str = REQUEST_PATH) -> dict[str, Any]:
    root = root.resolve()
    event_before = _exact_sha(event_before, "event_before")
    event_after = _exact_sha(event_after, "event_after")
    if _git(root, "rev-parse", "HEAD") != event_after:
        raise DeployRequestError("head_not_event_after")
    parents = _git(root, "show", "-s", "--format=%P", event_after).split()
    if parents != [event_before]:
        raise DeployRequestError("approval_commit_must_have_exactly_one_expected_parent")
    changed = [
        line for line in _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", event_after).splitlines()
        if line
    ]
    if changed != [request_path]:
        raise DeployRequestError("approval_commit_must_only_change_request_file")
    request = load_request(root / request_path, event_before=event_before, event_after=event_after)
    return {
        "schema": "polymarket_v7_paper_deploy_push_validation_v1",
        "valid": True,
        "request_id": request["request_id"],
        "expected_parent_sha": event_before,
        "expected_deploy_sha": event_after,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "freeze_maker_forward_window": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--event-before", required=True)
    parser.add_argument("--event-after", required=True)
    parser.add_argument("--request", default=REQUEST_PATH)
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        result = validate_commit(
            args.repository_root,
            event_before=args.event_before,
            event_after=args.event_after,
            request_path=args.request,
        )
    except (DeployRequestError, OSError) as exc:
        parser.exit(2, f"v7_deploy_push_request: {exc}\n")
    lines = (
        f"EXPECTED_DEPLOY_SHA={result['expected_deploy_sha']}\n"
        "FREEZE_MAKER_FORWARD_WINDOW=false\n"
        f"V7_DEPLOY_REQUEST_ID={result['request_id']}\n"
    )
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8") as handle:
            handle.write(lines)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write("ready=true\n")
            handle.write(f"request_id={result['request_id']}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
