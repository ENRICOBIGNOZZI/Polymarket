#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def legacy_event_id_for_seed(family: str, direction: str, seed: int) -> str:
    code = (
        "family = " + repr(family) + "\n"
        "direction = " + repr(direction) + "\n"
        "print('STRUCT:' + str(abs(hash((family, direction)))))\n"
    )
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = str(seed)
    return subprocess.check_output(
        [sys.executable, "-c", code],
        env=env,
        text=True,
    ).strip()


def stable_event_id(family: str, direction: str) -> str:
    payload = json.dumps(
        {
            "direction": direction.strip().upper(),
            "family": " ".join(family.split()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"STRUCT:{digest}"


def audit(family: str, direction: str) -> dict[str, object]:
    legacy = {str(seed): legacy_event_id_for_seed(family, direction, seed) for seed in (1, 2, 3)}
    stable = stable_event_id(family, direction)
    unique_legacy = len(set(legacy.values()))
    return {
        "family": family,
        "direction": direction,
        "legacy_event_ids_by_pythonhashseed": legacy,
        "legacy_unique_ids": unique_legacy,
        "stable_event_id": stable,
        "defect_reproduced": unique_legacy > 1,
        "risk": (
            "Python hash randomization changes synthetic STRUCT event_id across process restarts, "
            "which can fragment event-level risk/state and defeat restart-stable identity."
        ),
        "successor_contract": [
            "derive synthetic structural event identity from deterministic canonical metadata",
            "use a stable cryptographic digest rather than Python hash()",
            "persist the canonical risk_event_id across scanner/broker/state boundaries",
            "fail closed if the canonical relation identity cannot be reconstructed",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit restart stability of synthetic structural event IDs")
    parser.add_argument(
        "--family",
        default="will bitcoin be <direction> <threshold> by <year>|august 2026",
    )
    parser.add_argument("--direction", default="UP")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.family, args.direction)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 1 if not result["defect_reproduced"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
