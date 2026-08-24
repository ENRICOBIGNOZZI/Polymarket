#!/usr/bin/env python3
"""Prepare a reviewed-config promotion from a promotion-ready research report.

This script only rewrites the versioned alpha configuration. It never pushes,
merges, deploys or submits orders; the scheduled workflow uses its output to
open a draft pull request.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from alpha_research import SCHEMA, load_config


def atomic_write(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def choose(report: dict[str, Any]) -> dict[str, Any]:
    ready = [x for x in report.get("candidates", []) if x.get("stage") == "promotion_ready"]
    if not ready:
        raise ValueError("report contains no promotion-ready candidate")
    ready.sort(
        key=lambda x: (
            float(x.get("promotion", {}).get("incremental_stressed_mean_return", 0.0)),
            float(x.get("promotion", {}).get("incremental_mean_return", 0.0)),
            float(x.get("screen", {}).get("absolute_improvement", 0.0)),
        ),
        reverse=True,
    )
    return ready[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--config", type=Path, default=Path("config/alpha_research.json"))
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--expected-source-sha", default="")
    ap.add_argument("--now", type=int, default=None)
    args = ap.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    if report.get("schema") != SCHEMA:
        raise ValueError("unexpected report schema")
    if report.get("production_modified") is not False:
        raise ValueError("research report violated the production isolation contract")
    source_sha = str(report.get("source_sha", ""))
    if args.expected_source_sha and source_sha != args.expected_source_sha:
        raise ValueError("research report source SHA does not match checked-out main")

    # Validate the current file before editing, then load the raw JSON so comments
    # are not silently invented and unrelated fields are preserved.
    validated = load_config(args.config)
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    selected = choose(report)
    family = selected["family"]
    candidate_id = selected["id"]
    challenger = next(
        (x for x in raw.get("challengers", []) if x.get("id") == candidate_id and x.get("family") == family),
        None,
    )
    if challenger is None:
        raise ValueError("promotion-ready candidate is not declared in current config")

    champion = raw["champions"][family]
    previous_params = dict(validated["_champions"][family].params)
    previous_execution_min_edge = float(validated["_champions"][family].execution_min_edge)
    champion["params"] = dict(selected["params"])
    champion["execution_min_edge"] = float(selected.get("execution_min_edge", champion.get("execution_min_edge", 0.001)))
    champion["hypothesis"] = f"Promoted from {candidate_id}: {selected.get('hypothesis', '')}".strip()

    raw["challengers"] = [x for x in raw["challengers"] if x.get("id") != candidate_id]
    rollback_id = f"rollback_{family.lower()}_{int(args.now or time.time())}"
    raw["challengers"].append({
        "id": rollback_id,
        "family": family,
        "hypothesis": f"Rollback reference for the champion preceding {candidate_id}.",
        "params": previous_params,
        "execution_min_edge": previous_execution_min_edge,
    })
    raw["last_promotion"] = {
        "candidate_id": candidate_id,
        "family": family,
        "source_sha": source_sha,
        "research_cycle": report.get("cycle_index"),
        "promoted_at": int(args.now or time.time()),
        "evidence": selected.get("promotion", {}),
    }

    output = args.output or args.config
    atomic_write(output, raw)
    # Re-validate the promoted result before a workflow can commit it.
    load_config(output)
    print(json.dumps({"candidate_id": candidate_id, "family": family, "output": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
