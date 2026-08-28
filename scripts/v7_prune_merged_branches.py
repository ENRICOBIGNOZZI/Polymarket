#!/usr/bin/env python3
"""Prune only remote branches whose tips are already ancestors of main.

Git history on main remains the archive. Protected refs and governance prefixes
are never deletion candidates. The default is dry-run; CI must pass --apply.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
PROTECTED_EXACT = {"HEAD", "main", "paper-validated"}
PROTECTED_PREFIXES = ("release/", "hotfix/")


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="main")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.repository_root.resolve()
    git(root, "fetch", "--prune", "--no-tags", args.remote,
        "+refs/heads/*:refs/remotes/origin/*")
    candidates = merged_remote_branches(root, args.remote, args.base)
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
        "schema": "polymarket_v7_merged_branch_cleanup_v1",
        "base": f"{args.remote}/{args.base}",
        "apply": args.apply,
        "protected_exact": sorted(PROTECTED_EXACT),
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "deleted_count": len(deleted),
        "deleted": deleted,
        "failures": failures,
        "unmerged_branches_deleted": 0,
    }
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
