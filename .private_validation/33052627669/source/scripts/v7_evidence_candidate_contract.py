#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise SystemExit(message)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{path} must contain an object")
    return value


def safe_relative(value: object, field: str) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        fail(f"invalid {field}: {text!r}")
    return text


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def validate(candidate_root: Path, main_root: Path, expected_sha: str | None = None) -> dict[str, str]:
    candidate_root = candidate_root.resolve()
    main_root = main_root.resolve()
    cfg = load_json(main_root / "config/v7_evidence_runtime.json")
    if cfg.get("schema_version") != 2:
        fail("evidence runtime schema_version must be 2")
    if cfg.get("paper_only") is not True or cfg.get("authenticated_execution") is not False:
        fail("evidence runtime itself must be PAPER-only with authenticated execution disabled")

    contract = cfg.get("candidate_contract")
    if not isinstance(contract, dict):
        fail("candidate_contract must be an object")

    manifest = load_json(candidate_root / "config/live_champion.json")
    if contract.get("require_enabled_v7_champion") is True:
        if manifest.get("enabled") is not True or manifest.get("version") != 7:
            fail("candidate must already contain the final enabled V7 champion")
    if manifest.get("paper_only") is not True:
        fail("candidate champion must be PAPER-only")
    if manifest.get("authenticated_execution") is not False:
        fail("candidate champion authenticated_execution must be false")

    loop_rel = safe_relative(manifest.get("loop"), "champion loop")
    config_rel = safe_relative(manifest.get("config"), "champion config")
    run_root_rel = safe_relative(manifest.get("run_root"), "champion run_root")
    expected_loop = str(contract.get("champion_loop") or "")
    expected_config = str(contract.get("champion_config") or "")
    expected_run_root = str(contract.get("champion_run_root") or "")
    if loop_rel != expected_loop or config_rel != expected_config or run_root_rel != expected_run_root:
        fail(
            "candidate champion paths are not canonical: "
            f"loop={loop_rel!r} config={config_rel!r} run_root={run_root_rel!r}"
        )

    required_paths = (
        loop_rel,
        config_rel,
        "scripts/paper_v7_execution_loop.sh",
        "scripts/runtime_singleton_launcher.py",
        "scripts/v7_execution_evidence.py",
        "scripts/v7_execution_evidence_hardened.py",
        "config/v7_frequency_matrix.json",
        "config/v7_execution_evidence.json",
    )
    for rel in required_paths:
        if not (candidate_root / rel).is_file():
            fail(f"candidate missing required V7 primitive: {rel}")

    paper = load_json(candidate_root / config_rel)
    if paper.get("engine_version") != 7 or paper.get("paper_only") is not True:
        fail("candidate paper config must be engine_version=7 and paper_only=true")
    v7 = paper.get("v7")
    if not isinstance(v7, dict):
        fail("candidate paper config v7 section missing")
    if v7.get("paper_only") is not True or v7.get("authenticated_execution") is not False:
        fail("candidate V7 config must be PAPER-only with authenticated execution disabled")
    if contract.get("require_authoritative_fee") is True and v7.get("authoritative_fee_required") is not True:
        fail("candidate must require authoritative fees")
    if contract.get("require_shared_execution_ledger") is True and v7.get("shared_execution_ledger_required") is not True:
        fail("candidate must require the shared execution ledger")
    if contract.get("require_joint_fill_state_for_multileg") is True and v7.get("joint_fill_state_required_for_multileg") is not True:
        fail("candidate must require empirical multi-leg joint fill state")
    if float(paper.get("max_drawdown", 1.0)) > float(contract.get("max_drawdown", 0.15)) + 1e-12:
        fail("candidate max_drawdown exceeds evidence contract")

    if contract.get("require_operator_directives_match_main") is True:
        candidate_directives = (candidate_root / "config/operator_directives.json").read_bytes()
        main_directives = (main_root / "config/operator_directives.json").read_bytes()
        if candidate_directives != main_directives:
            fail("candidate operator_directives.json must exactly match current main")

    head = git(candidate_root, "rev-parse", "HEAD")
    if expected_sha is not None:
        if not SHA40.fullmatch(expected_sha):
            fail(f"invalid expected SHA: {expected_sha!r}")
        if head != expected_sha:
            fail(f"candidate checkout {head} != expected source SHA {expected_sha}")

    return {
        "source_sha": head,
        "paper_only": "true",
        "authenticated_execution": "false",
        "champion_version": "7",
        "loop": loop_rel,
        "config": config_rel,
        "run_root": run_root_rel,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a canonical V7 isolated PAPER evidence candidate")
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--main-root", type=Path, required=True)
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    result = validate(args.candidate_root, args.main_root, args.expected_sha)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
