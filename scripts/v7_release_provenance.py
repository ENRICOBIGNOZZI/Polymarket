#!/usr/bin/env python3
"""Read-only exact-SHA release provenance record; never signs or deploys."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def report(root: Path) -> dict:
    sha = git(root, "rev-parse", "HEAD")
    status = git(root, "status", "--porcelain")
    files = ["config/paper_v7.json", "config/v7_execution_modes.json", "config/v7_live_caps_zero.json", "config/v7_platform_contract.json"]
    hashes = {item: hashlib.sha256((root / item).read_bytes()).hexdigest() for item in files}
    return {"schema_version": 1, "exact_code_sha": sha, "worktree_clean": not bool(status),
            "configuration_hashes": hashes, "signed_release_verified": False,
            "state": "MORE_EVIDENCE_REQUIRED", "limitations": ["Release signatures and hosted CI provenance require external verification."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(); rendered = json.dumps(report(args.repository_root.resolve()), sort_keys=True, indent=2) + "\n"
    if args.output: args.output.write_text(rendered, encoding="utf-8")
    else: print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
