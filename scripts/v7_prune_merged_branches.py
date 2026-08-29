#!/usr/bin/env python3
"""Prune remote branches proven merged into main.

The proof may be either Git ancestry or a merged pull request in this repository.
Squash merges intentionally do not preserve head ancestry, so ancestry alone leaves
hundreds of already-merged branches behind.  Open PR heads and bases, protected
refs, and governance prefixes are never deletion candidates.  The default is a
dry-run; CI must pass --apply.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
PROTECTED_EXACT = {"HEAD", "main", "telemetry"}
PROTECTED_PREFIXES = ("release/", "hotfix/")
RESEARCH_ARCHIVE_PREFIXES = ("research/", "experiment/", "improve/", "agent/", "review/")
DISPOSABLE_BRANCH = re.compile(r"^tmp-unused(?:-[0-9]+)?$")


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], text=True, capture_output=True, check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def merged_remote_branches(root: Path, remote: str, base: str) -> list[str]:
    output = git(root, "for-each-ref", "--format=%(refname:strip=3)",
                 f"--merged={remote}/{base}", f"refs/remotes/{remote}")
    candidates = []
    for raw in output.splitlines():
        branch = raw.strip()
        if (not branch or branch in PROTECTED_EXACT or branch.startswith(PROTECTED_PREFIXES)
                or not SAFE_BRANCH.fullmatch(branch) or ".." in branch or branch.startswith("/")):
            continue
        candidates.append(branch)
    return sorted(set(candidates))


def is_protected_branch(branch: str) -> bool:
    return (
        not branch
        or branch in PROTECTED_EXACT
        or branch.startswith(PROTECTED_PREFIXES)
        or not SAFE_BRANCH.fullmatch(branch)
        or ".." in branch
        or branch.startswith("/")
    )


def remote_branches(root: Path, remote: str) -> dict[str, str]:
    output = git(
        root,
        "for-each-ref",
        "--format=%(refname:strip=3) %(objectname)",
        f"refs/remotes/{remote}",
    )
    branches: dict[str, str] = {}
    for line in output.splitlines():
        branch, separator, sha = line.strip().partition(" ")
        if branch and separator and sha:
            branches[branch] = sha
    return branches


def merged_pr_branches(
    branches: dict[str, str], pull_requests: list[dict[str, Any]], repository: str,
) -> list[str]:
    """Return same-repository merged PR heads unused by any open PR.

    A branch that is an open PR head or base is retained even if an older PR from
    that branch was merged.  Closed-but-unmerged PRs are deliberately not proof.
    """
    open_refs: set[str] = set()
    for pull in pull_requests:
        if pull.get("state") != "open":
            continue
        for side in ("head", "base"):
            ref = (pull.get(side) or {}).get("ref")
            if ref:
                open_refs.add(str(ref))

    merged_refs: set[str] = set()
    merged_head_shas: set[str] = set()
    for pull in pull_requests:
        head = pull.get("head") or {}
        head_repo = head.get("repo") or {}
        branch = str(head.get("ref") or "")
        if (
            not pull.get("merged_at")
            or head_repo.get("full_name") != repository
        ):
            continue
        merged_refs.add(branch)
        head_sha = str(head.get("sha") or "")
        if head_sha:
            merged_head_shas.add(head_sha)

    candidates: set[str] = set()
    for branch, sha in branches.items():
        if branch in open_refs or is_protected_branch(branch):
            continue
        if branch in merged_refs or sha in merged_head_shas or DISPOSABLE_BRANCH.fullmatch(branch):
            candidates.add(branch)
    return sorted(candidates)


def closed_operational_pr_branches(
    branches: dict[str, str], pull_requests: list[dict[str, Any]], repository: str,
) -> list[str]:
    """Return closed-unmerged operational heads while preserving research archives."""
    open_refs: set[str] = set()
    for pull in pull_requests:
        if pull.get("state") != "open":
            continue
        for side in ("head", "base"):
            ref = (pull.get(side) or {}).get("ref")
            if ref:
                open_refs.add(str(ref))

    candidates: set[str] = set()
    for pull in pull_requests:
        head = pull.get("head") or {}
        head_repo = head.get("repo") or {}
        branch = str(head.get("ref") or "")
        if (
            pull.get("state") != "closed"
            or pull.get("merged_at")
            or head_repo.get("full_name") != repository
            or branch not in branches
            or branch in open_refs
            or branch.startswith(RESEARCH_ARCHIVE_PREFIXES)
            or is_protected_branch(branch)
        ):
            continue
        candidates.add(branch)
    return sorted(candidates)


def fetch_pull_requests(repository: str, token: str | None) -> list[dict[str, Any]]:
    pulls: list[dict[str, Any]] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{repository}/pulls"
            f"?state=all&per_page=100&page={page}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "polymarket-v7-branch-hygiene",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                page_items = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"GitHub pull-request inventory failed: {error}") from error
        if not isinstance(page_items, list):
            raise RuntimeError("GitHub pull-request inventory returned a non-list response")
        pulls.extend(page_items)
        if len(page_items) < 100:
            return pulls
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="main")
    parser.add_argument("--github-repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--include-merged-pr-branches", action="store_true")
    parser.add_argument("--include-closed-operational-branches", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.repository_root.resolve()
    git(root, "fetch", "--prune", "--no-tags", args.remote,
        "+refs/heads/*:refs/remotes/origin/*")
    ancestry_candidates = merged_remote_branches(root, args.remote, args.base)
    pr_candidates: list[str] = []
    closed_operational_candidates: list[str] = []
    pull_request_count = 0
    if args.include_merged_pr_branches or args.include_closed_operational_branches:
        if not args.github_repository:
            raise RuntimeError("--github-repository is required for PR-aware cleanup")
        pulls = fetch_pull_requests(args.github_repository, os.environ.get("GITHUB_TOKEN"))
        pull_request_count = len(pulls)
        branches = remote_branches(root, args.remote)
    if args.include_merged_pr_branches:
        pr_candidates = merged_pr_branches(
            branches, pulls, args.github_repository,
        )
    if args.include_closed_operational_branches:
        closed_operational_candidates = closed_operational_pr_branches(
            branches, pulls, args.github_repository,
        )
    candidates = sorted(
        set(ancestry_candidates) | set(pr_candidates) | set(closed_operational_candidates)
    )
    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    if args.apply:
        for branch in candidates:
            try:
                git(root, "push", args.remote, "--delete", branch)
                deleted.append(branch)
            except RuntimeError as error:
                failures.append({"branch": branch, "error": str(error)})
    report = {
        "schema": "polymarket_v7_merged_branch_cleanup_v3",
        "base": f"{args.remote}/{args.base}",
        "apply": args.apply,
        "protected_exact": sorted(PROTECTED_EXACT),
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "pull_request_count": pull_request_count,
        "ancestry_candidate_count": len(ancestry_candidates),
        "merged_pr_candidate_count": len(pr_candidates),
        "closed_operational_candidate_count": len(closed_operational_candidates),
        "ancestry_candidates": ancestry_candidates,
        "merged_pr_candidates": pr_candidates,
        "closed_operational_candidates": closed_operational_candidates,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "deleted_count": len(deleted),
        "deleted": deleted,
        "failures": failures,
        "unmerged_branches_deleted": len(set(deleted) & set(closed_operational_candidates)),
    }
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
