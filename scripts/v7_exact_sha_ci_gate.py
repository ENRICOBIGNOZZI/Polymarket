#!/usr/bin/env python3
"""Fail closed unless the exact revision has current successful V7 CI checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_exact_sha_ci_receipt_v1"
DEFAULT_REQUIRED = ("ci-v7-Release", "ci-v7-Debug")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def latest_runs(check_runs: list[dict[str, Any]], required: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for name in required:
        matches = [row for row in check_runs if row.get("name") == name]
        if matches:
            selected[name] = max(
                matches,
                key=lambda row: (str(row.get("completed_at") or row.get("started_at") or ""),
                                 int(row.get("id") or 0)),
            )
    return selected


def receipt(repository: str, sha: str, check_runs: list[dict[str, Any]], now: int,
            required: tuple[str, ...] = DEFAULT_REQUIRED) -> dict[str, Any]:
    selected = latest_runs(check_runs, required)
    checks: dict[str, dict[str, Any]] = {}
    for name in required:
        row = selected.get(name, {})
        checks[name] = {
            "id": row.get("id"),
            "status": row.get("status"),
            "conclusion": row.get("conclusion"),
            "completed_at": row.get("completed_at"),
            "details_url": row.get("details_url"),
        }
    green = all(
        checks[name]["status"] == "completed" and checks[name]["conclusion"] == "success"
        for name in required
    )
    return {
        "schema": SCHEMA,
        "verified_at": now,
        "repository": repository,
        "sha": sha,
        "required_checks": list(required),
        "checks": checks,
        "exact_sha_ci_green": green,
    }


def fetch_check_runs(repository: str, sha: str, timeout: float) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repository}/commits/{sha}/check-runs?per_page=100"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "polymarket-v7-exact-sha-gate",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"exact_sha_ci_query_failed:{exc}") from exc
    rows = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("exact_sha_ci_invalid_response")
    return rows


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()
    if not REPOSITORY_RE.fullmatch(args.repository):
        raise SystemExit("repository must be owner/name")
    if not SHA_RE.fullmatch(args.sha):
        raise SystemExit("sha must be an exact lowercase 40-character revision")
    if not args.timeout_seconds > 0.0:
        raise SystemExit("timeout-seconds must be positive")
    try:
        value = receipt(args.repository, args.sha,
                        fetch_check_runs(args.repository, args.sha, args.timeout_seconds),
                        int(time.time()))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    atomic_write(args.output, value)
    if value["exact_sha_ci_green"] is not True:
        print("exact_sha_ci_not_green", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
