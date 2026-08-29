#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: str) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def validate_base(
    *,
    main_sha: str,
    validated_sha: str,
    checkout_sha: str,
    validated_is_ancestor: bool,
    champion: dict,
    directives: dict,
) -> str:
    if checkout_sha != main_sha:
        raise ValueError("workflow checkout is not current main")
    if not validated_is_ancestor:
        raise ValueError("paper-validated is not an ancestor of current main")

    architecture = directives.get("architecture") or {}
    if architecture.get("operational_champion_may_be_absent") is not True:
        raise ValueError("operator directives do not authorize a no-champion cutover phase")

    if champion.get("paper_only") is not True:
        raise ValueError("live champion manifest must remain PAPER-only")
    if champion.get("authenticated_execution") is not False:
        raise ValueError("authenticated execution must remain disabled")

    if champion.get("enabled") is True:
        if champion.get("version") != 7:
            raise ValueError("enabled operational champion must be V7")
        if main_sha != validated_sha:
            raise ValueError("an enabled operational champion requires main == paper-validated before integration")
        return "validated_incumbent"

    if champion.get("enabled") is not False:
        raise ValueError("live champion enabled flag must be boolean")
    for key in ("version", "loop", "config", "run_root"):
        if champion.get(key) is not None:
            raise ValueError(f"disabled no-champion manifest must have null {key}")
    return "v7_no_champion_cutover"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the safe base state for the single V7 integration merge authority")
    parser.add_argument("--main-sha", required=True)
    parser.add_argument("--validated-sha", required=True)
    parser.add_argument("--checkout-sha", required=True)
    parser.add_argument("--validated-is-ancestor", choices=("true", "false"), required=True)
    parser.add_argument("--champion", default="config/live_champion.json")
    parser.add_argument("--directives", default="config/operator_directives.json")
    args = parser.parse_args()

    try:
        mode = validate_base(
            main_sha=args.main_sha,
            validated_sha=args.validated_sha,
            checkout_sha=args.checkout_sha,
            validated_is_ancestor=args.validated_is_ancestor == "true",
            champion=load_json(args.champion),
            directives=load_json(args.directives),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"integration_base_gate=blocked reason={exc}")
        return 1

    print(f"integration_base_gate=ready mode={mode} main_sha={args.main_sha} paper_validated_sha={args.validated_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
