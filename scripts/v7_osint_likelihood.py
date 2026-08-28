#!/usr/bin/env python3
"""Fit frozen chronological OSINT event-family likelihood challengers."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from v7_osint_engine import LikelihoodObservation, OsintError, fit_likelihood_model

SCHEMA = "polymarket_v7_osint_likelihood_registry_v1"


def load_observations(path: Path) -> tuple[LikelihoodObservation, ...]:
    rows: list[LikelihoodObservation] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                row = LikelihoodObservation(
                    event_family=str(raw["event_family"]),
                    root_lineage_id=str(raw["root_lineage_id"]),
                    observed_ts_ms=int(raw["observed_ts_ms"]),
                    event_direction=int(raw["event_direction"]),
                    outcome=int(raw["outcome"]),
                    prior=float(raw["prior"]),
                )
                row.validate(); rows.append(row)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OsintError) as exc:
                raise OsintError(f"invalid_likelihood_label_line:{line_number}") from exc
    if not rows:
        raise OsintError("likelihood_label_tape_empty")
    return tuple(rows)


def build_registry(
    rows: Sequence[LikelihoodObservation], *, model_sha: str, cutoff_ms: int,
    embargo_ms: int, minimum_oos_events: int,
) -> dict[str, Any]:
    if len(model_sha) != 40 or any(x not in "0123456789abcdef" for x in model_sha.lower()):
        raise OsintError("model_sha_must_be_exact_hex")
    models: list[dict[str, Any]] = []
    for family in sorted({x.event_family for x in rows}):
        try:
            model, report = fit_likelihood_model(
                family, rows, trained_until_ms=cutoff_ms, oos_rows=rows,
                minimum_oos_events=minimum_oos_events, embargo_ms=embargo_ms,
            )
            models.append({
                "event_family": family,
                "model": asdict(model),
                "oos": None if report is None else asdict(report),
                "status": "ENGINEERING_VALIDATED" if model.oos_validated else "FORWARD_EVIDENCE_PENDING",
                "execution_authority": False,
            })
        except OsintError as exc:
            models.append({
                "event_family": family, "model": None, "oos": None,
                "status": "IMPLEMENTATION_PARTIAL", "blocker": str(exc),
                "execution_authority": False,
            })
    return {
        "schema": SCHEMA,
        "model_sha": model_sha,
        "training_cutoff_ms": cutoff_ms,
        "embargo_ms": embargo_ms,
        "independent_sample_unit": "root_lineage_id",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "automatic_promotion": False,
        "event_families": models,
    }


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--cutoff-ms", type=int, required=True)
    parser.add_argument("--embargo-ms", type=int, default=0)
    parser.add_argument("--minimum-oos-events", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    value = build_registry(
        load_observations(args.labels), model_sha=args.model_sha.lower(),
        cutoff_ms=args.cutoff_ms, embargo_ms=max(0, args.embargo_ms),
        minimum_oos_events=max(1, args.minimum_oos_events),
    )
    atomic_write(args.output, value)
    print(json.dumps({
        "schema": SCHEMA, "models": len(value["event_families"]),
        "oos_validated": sum(x.get("status") == "ENGINEERING_VALIDATED" for x in value["event_families"]),
        "automatic_promotion": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
