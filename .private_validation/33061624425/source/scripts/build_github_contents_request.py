#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path


def build_payload(input_path: Path, message: str, branch: str, sha: str = "") -> dict[str, str]:
    payload = {
        "message": message,
        "content": base64.b64encode(input_path.read_bytes()).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a GitHub Contents API request body without putting file bytes on argv"
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--sha", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    payload = build_payload(Path(args.input), args.message, args.branch, args.sha)
    Path(args.output).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
