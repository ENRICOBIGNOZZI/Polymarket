#!/usr/bin/env python3
"""Redacting high-entropy scanner for the worktree and reachable git history.

This scanner intentionally uses a different detection method from the pattern
scanner: it looks for token-shaped strings with suspicious entropy.  Findings
contain only a SHA-256 fingerprint and location, never candidate bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TEXT_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp", ".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}
SELF_TEST_FIXTURES = frozenset({"tests/test_v7_secret_scan.py", "tests/test_v7_entropy_secret_scan.py"})
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?(?:key|secret)|access[_-]?token|auth(?:orization)?|password|"
    r"private[_-]?key|session[_-]?(?:key|token)|credential)\b\s*[=:]\s*[\"']?([A-Za-z0-9+/_=-]{32,})"
)
JWT_RE = re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")
MIN_ENTROPY = 3.5


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "location": self.location, "fingerprint": self.fingerprint}


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _entropy(value: str) -> float:
    length = len(value)
    return -sum((value.count(char) / length) * math.log2(value.count(char) / length) for char in set(value))


def _looks_like_hash(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32,}", value))


def scan_text(text: str, *, location: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in [*SENSITIVE_ASSIGNMENT_RE.finditer(line), *JWT_RE.finditer(line)]:
            token = match.group(1)
            if not _looks_like_hash(token) and _entropy(token) >= MIN_ENTROPY:
                findings.append(Finding("high_entropy_token", f"{location}:{line_number}", _fingerprint(token)))
    return findings


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL)


def scan_worktree(root: Path) -> list[Finding]:
    root = Path(root).resolve()
    findings: list[Finding] = []
    files = set(_git(root, "ls-files", "--cached", "--others", "--exclude-standard").splitlines())
    for relative in sorted(files - SELF_TEST_FIXTURES):
        path = root / relative
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            findings.extend(scan_text(path.read_text(encoding="utf-8"), location=relative))
        except UnicodeDecodeError:
            continue
    return findings


def scan_history(root: Path, commits: Iterable[str] | None = None) -> list[Finding]:
    root = Path(root).resolve()
    blobs: dict[str, str] = {}
    if commits is not None:
        for revision in commits:
            for line in _git(root, "ls-tree", "-r", "--full-tree", revision).splitlines():
                try:
                    _, kind, object_and_path = line.split(" ", 2)
                    object_id, relative = object_and_path.split("\t", 1)
                except ValueError:
                    continue
                if kind == "blob" and relative not in SELF_TEST_FIXTURES and Path(relative).suffix.lower() in TEXT_SUFFIXES:
                    blobs.setdefault(object_id, relative)
    else:
        for line in _git(root, "rev-list", "--objects", "--all").splitlines():
            object_id, _, relative = line.partition(" ")
            if relative and relative not in SELF_TEST_FIXTURES and Path(relative).suffix.lower() in TEXT_SUFFIXES:
                blobs.setdefault(object_id, relative)
    if not blobs:
        return []
    batch = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"], input=("\n".join(blobs) + "\n").encode("ascii"),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    ).stdout
    findings: list[Finding] = []
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
        offset += size + 1
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(text, location=f"history:{header[0][:12]}:{blobs.get(header[0], 'unknown')}"))
    return findings


def report(root: Path, *, include_history: bool = False) -> dict[str, object]:
    root = Path(root).resolve()
    rows = [*scan_worktree(root), *(scan_history(root) if include_history else [])]
    dedup = {(row.kind, row.location, row.fingerprint): row for row in rows}
    findings = [row.as_dict() for row in sorted(dedup.values(), key=lambda row: (row.location, row.fingerprint))]
    return {
        "schema": "polymarket_v7_entropy_secret_scan_v1", "repository": str(root),
        "history_scanned": include_history, "findings": findings, "finding_count": len(findings),
        "safe_for_authenticated_execution": len(findings) == 0,
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
