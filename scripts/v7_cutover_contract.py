#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _fail(message: str) -> None:
    raise SystemExit(message)


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"{path} must contain a JSON object")
    return value


def _safe_relative(value: object, field: str) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        _fail(f"invalid {field}: {text!r}")
    return text


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def validate(root: Path, expected_head: str | None) -> dict[str, str]:
    root = root.resolve()
    manifest = _load_json(root / "config/live_champion.json")
    if manifest.get("enabled") is not True:
        _fail("V7 cutover blocked: live champion is not explicitly enabled")
    version = manifest.get("version")
    if isinstance(version, bool) or version != 7:
        _fail(f"V7 cutover blocked: champion version must be exactly 7, got {version!r}")
    if manifest.get("paper_only") is not True:
        _fail("V7 cutover blocked: champion manifest paper_only must be true")
    if manifest.get("authenticated_execution") is not False:
        _fail("V7 cutover blocked: champion manifest authenticated_execution must be false")

    loop_rel = _safe_relative(manifest.get("loop"), "champion loop")
    config_rel = _safe_relative(manifest.get("config"), "champion config")
    run_root_rel = _safe_relative(manifest.get("run_root"), "champion run_root")
    if loop_rel != "scripts/paper_v7_loop.sh":
        _fail(f"V7 cutover blocked: canonical loop must be scripts/paper_v7_loop.sh, got {loop_rel}")
    if config_rel != "config/paper_v7.json":
        _fail(f"V7 cutover blocked: canonical config must be config/paper_v7.json, got {config_rel}")
    if run_root_rel != "runs/paper_v7_live":
        _fail(f"V7 cutover blocked: canonical run_root must be runs/paper_v7_live, got {run_root_rel}")

    for rel in (
        loop_rel,
        config_rel,
        "scripts/paper_v7_execution_loop.sh",
        "scripts/v7_runtime_status.py",
        "config/v7_frequency_matrix.json",
        "config/v7_execution_evidence.json",
    ):
        if not (root / rel).is_file():
            _fail(f"V7 cutover blocked: required file missing: {rel}")

    cfg = _load_json(root / config_rel)
    if cfg.get("engine_version") != 7:
        _fail("V7 cutover blocked: engine_version must be 7")
    if cfg.get("paper_only") is not True:
        _fail("V7 cutover blocked: paper_only must be true")
    v7 = cfg.get("v7")
    if not isinstance(v7, dict):
        _fail("V7 cutover blocked: config.v7 must be an object")
    for key in (
        "paper_only",
        "authoritative_fee_required",
        "shared_execution_ledger_required",
        "joint_fill_state_required_for_multileg",
    ):
        if v7.get(key) is not True:
            _fail(f"V7 cutover blocked: v7.{key} must be true")
    if v7.get("authenticated_execution") is not False:
        _fail("V7 cutover blocked: v7.authenticated_execution must be false")
    if float(cfg.get("max_drawdown", 1.0)) > 0.15 + 1e-12:
        _fail("V7 cutover blocked: max_drawdown exceeds 15%")

    head = _git(root, "rev-parse", "HEAD")
    if expected_head is not None:
        if not SHA40.fullmatch(expected_head):
            _fail(f"invalid expected SHA: {expected_head!r}")
        if head != expected_head:
            _fail(f"V7 cutover blocked: checkout {head} != expected {expected_head}")

    return {
        "V7_CUTOVER_SHA": head,
        "V7_CHAMPION_VERSION": "7",
        "V7_CHAMPION_LOOP": loop_rel,
        "V7_CHAMPION_CONFIG": config_rel,
        "V7_CHAMPION_RUN_ROOT": run_root_rel,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed V7 PAPER cutover contract")
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--expected-head")
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    env = validate(args.repository_root, args.expected_head)
    output = "\n".join(f"{key}={value}" for key, value in env.items()) + "\n"
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8") as handle:
            handle.write(output)
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
