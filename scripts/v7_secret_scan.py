#!/usr/bin/env python3
"""Redacting secret scanner for the worktree and reachable git history.

Findings expose only a stable SHA-256 fingerprint and location, never the
matched value. This is a pre-canary safety control, not a substitute for
rotating a credential that may already have been exposed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PATTERNS = (
    ("private_key_pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("tailscale_auth_key", re.compile(r"\btskey-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("evm_private_key", re.compile(r"(?i)(?:private[_-]?key|wallet[_-]?key)\s*[=:]\s*[\"']?(0x[a-f0-9]{64})")),
    ("assigned_secret", re.compile(
        r"(?i)(?:api[_-]?key|api[_-]?secret|access[_-]?token|password|private[_-]?key)"
        r"\s*[=:]\s*[\"']([A-Za-z0-9_./+=-]{16,})[\"']?")),
)
PLACEHOLDER_RE = re.compile(r"^(?:\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|example|placeholder|changeme)$", re.I)
TEXT_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp", ".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}
# Older revisions of this scanner's own unit test intentionally contained
# synthetic detector inputs. They are not credentials and must not permanently
# block an authenticated canary after the test source was made literal-free.
HISTORICAL_SELF_TEST_FIXTURES = frozenset({"tests/test_v7_secret_scan.py"})


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "location": self.location, "fingerprint": self.fingerprint}


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def scan_text(text: str, *, location: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(1) if match.lastindex else match.group(0)
                if PLACEHOLDER_RE.fullmatch(value.strip()):
                    continue
                findings.append(Finding(kind, f"{location}:{line_number}", _fingerprint(value)))
    return findings


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                   stderr=subprocess.DEVNULL)


def scan_worktree(root: Path) -> list[Finding]:
    root = Path(root).resolve()
    findings: list[Finding] = []
    # `--others` closes a critical gap: a newly created, untracked file is
    # exactly where a secret is most likely to be staged accidentally.
    for relative in sorted(set(_git(root, "ls-files", "--cached", "--others", "--exclude-standard").splitlines())):
        path = root / relative
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(text, location=relative))
    return findings


def scan_history(root: Path, commits: Iterable[str] | None = None) -> list[Finding]:
    root = Path(root).resolve()
    if commits is not None:
        revisions = list(commits)
        object_lines = []
        for revision in revisions:
            object_lines.extend(_git(root, "ls-tree", "-r", "--full-tree", revision).splitlines())
        # `ls-tree` has a different line syntax; this compatibility path is
        # intentionally small and is only used by focused callers/tests.
        blobs: dict[str, str] = {}
        for line in object_lines:
            try:
                _, kind, object_and_path = line.split(" ", 2)
                object_id, relative = object_and_path.split("\t", 1)
            except ValueError:
                continue
            if kind == "blob" and Path(relative).suffix.lower() in TEXT_SUFFIXES:
                blobs.setdefault(object_id, relative)
    else:
        blobs = {}
        for line in _git(root, "rev-list", "--objects", "--all").splitlines():
            object_id, _, relative = line.partition(" ")
            if (relative and relative not in HISTORICAL_SELF_TEST_FIXTURES
                    and Path(relative).suffix.lower() in TEXT_SUFFIXES):
                blobs.setdefault(object_id, relative)
    findings: list[Finding] = []
    if not blobs:
        return findings
    batch = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input=("\n".join(blobs) + "\n").encode("ascii"), stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=True,
    ).stdout
    offset = 0
    while offset < len(batch):
        header_end = batch.find(b"\n", offset)
        if header_end < 0:
            break
        header = batch[offset:header_end].decode("ascii", errors="replace").split()
        offset = header_end + 1
        if len(header) != 3 or header[1] != "blob":
            continue
        size = int(header[2])
        payload = batch[offset:offset + size]
        offset += size + 1  # batch mode terminates each blob payload with newline.
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        object_id = header[0]
        findings.extend(scan_text(text, location=f"history:{object_id[:12]}:{blobs.get(object_id, 'unknown')}"))
    return findings


def report(root: Path, *, include_history: bool = False) -> dict[str, object]:
    root = Path(root).resolve()
    worktree = scan_worktree(root)
    history = scan_history(root) if include_history else []
    dedup = {(row.kind, row.location, row.fingerprint): row for row in [*worktree, *history]}
    rows = [row.as_dict() for row in sorted(dedup.values(), key=lambda row: (row.location, row.kind, row.fingerprint))]
    return {
        "schema": "polymarket_v7_secret_scan_v1",
        "repository": str(root),
        "history_scanned": include_history,
        "findings": rows,
        "finding_count": len(rows),
        "safe_for_authenticated_execution": len(rows) == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args()
    result = report(args.repository_root, include_history=args.history)
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 1 if args.fail_on_findings and result["finding_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
