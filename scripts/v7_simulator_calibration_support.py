#!/usr/bin/env python3
"""Admit PAPER simulation only inside an immutable, observed calibration cell.

The utility is offline and has no execution, signing, or market-data client.
It turns a maker-probe calibration report into a narrow research-only decision;
it never promotes a model or authorizes a live order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTEXT_FIELDS = ("queue_bucket", "spread_bucket", "tte_bucket", "volatility_bucket", "activity_bucket", "quote_lifetime_bucket")
REPORT_SCHEMA = "polymarket_v7_maker_probe_calibration_v1"
SCHEMA = "polymarket_v7_simulator_calibration_support_v1"


class CalibrationSupportError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(CONTEXT_FIELDS):
        raise CalibrationSupportError("context:shape")
    if any(not isinstance(value[key], str) or not value[key] for key in CONTEXT_FIELDS):
        raise CalibrationSupportError("context:invalid")
    return {key: value[key] for key in CONTEXT_FIELDS}


def _probability(value: Any, field: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise CalibrationSupportError(f"{field}:invalid")
    return float(value)


def validate_calibration(value: Any) -> dict[str, Any]:
    required = {"schema", "experiment_id", "model_sha", "cells", "terminal_probes", "pre_registered_assignments",
                "unresolved_assignments", "invalid_outcomes", "modes", "reason_codes", "state",
                "promotion_credit", "simulation_policy", "calibration_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise CalibrationSupportError("calibration:shape")
    if (value["schema"] != REPORT_SCHEMA or not isinstance(value["experiment_id"], str) or not value["experiment_id"]
            or not SHA_RE.fullmatch(str(value["model_sha"])) or value["state"] not in
            {"LIVE_CALIBRATION_EVIDENCE", "PAPER_DIAGNOSTIC_ONLY", "MORE_EVIDENCE_REQUIRED"}
            or value["promotion_credit"] is not False
            or value["simulation_policy"] != "use_conservative_any_fill_probability_only_when_cell_mature"):
        raise CalibrationSupportError("calibration:identity")
    if (not isinstance(value["modes"], list) or value["modes"] != sorted(set(value["modes"]))
            or any(mode not in {"PAPER", "LIVE_OBSERVED"} for mode in value["modes"])
            or not isinstance(value["reason_codes"], list) or any(not isinstance(code, str) or not code for code in value["reason_codes"])):
        raise CalibrationSupportError("calibration:metadata")
    for field in ("terminal_probes", "pre_registered_assignments", "unresolved_assignments", "invalid_outcomes"):
        if isinstance(value[field], bool) or not isinstance(value[field], int) or value[field] < 0:
            raise CalibrationSupportError(f"calibration:{field}")
    if not isinstance(value["cells"], list):
        raise CalibrationSupportError("calibration:cells")
    seen: set[tuple[str, ...]] = set()
    terminal = 0
    cell_required = {"context", "terminal_probes", "flow_reached", "filled_after_reach", "p_flow_reaches_quote",
                     "p_flow_reaches_quote_lower_95", "p_fill_given_reach", "p_fill_given_reach_lower_95",
                     "filled_size_fraction_given_reach", "conservative_any_fill_probability", "mature"}
    for cell in value["cells"]:
        if not isinstance(cell, dict) or set(cell) != cell_required:
            raise CalibrationSupportError("calibration:cell_shape")
        context = _context(cell["context"])
        key = tuple(context[field] for field in CONTEXT_FIELDS)
        if key in seen:
            raise CalibrationSupportError("calibration:duplicate_cell")
        seen.add(key)
        for field in ("terminal_probes", "flow_reached", "filled_after_reach"):
            if isinstance(cell[field], bool) or not isinstance(cell[field], int) or cell[field] < 0:
                raise CalibrationSupportError("calibration:cell_counts")
        if cell["flow_reached"] > cell["terminal_probes"] or cell["filled_after_reach"] > cell["flow_reached"]:
            raise CalibrationSupportError("calibration:cell_counts")
        for field in ("p_flow_reaches_quote", "p_flow_reaches_quote_lower_95", "p_fill_given_reach",
                      "p_fill_given_reach_lower_95", "filled_size_fraction_given_reach",
                      "conservative_any_fill_probability"):
            _probability(cell[field], f"calibration:{field}", allow_none=True)
        if not isinstance(cell["mature"], bool):
            raise CalibrationSupportError("calibration:cell_mature")
        terminal += cell["terminal_probes"]
    if terminal != value["terminal_probes"]:
        raise CalibrationSupportError("calibration:terminal_count")
    unhashed = dict(value)
    supplied = unhashed.pop("calibration_sha256")
    if not isinstance(supplied, str) or not SHA256_RE.fullmatch(supplied) or supplied != digest(unhashed):
        raise CalibrationSupportError("calibration:sha256")
    return value


def decide(calibration: Any, *, context: Any) -> dict[str, Any]:
    """Return a fail-closed research-only simulator support decision."""
    calibration = validate_calibration(calibration)
    context = _context(context)
    matching = [cell for cell in calibration["cells"] if cell["context"] == context]
    reasons: list[str] = []
    if calibration["state"] != "LIVE_CALIBRATION_EVIDENCE" or calibration["modes"] != ["LIVE_OBSERVED"]:
        reasons.append("calibration_not_live_observed")
    if calibration["invalid_outcomes"] or calibration["unresolved_assignments"] or calibration["reason_codes"]:
        reasons.append("calibration_incomplete")
    if not matching:
        reasons.append("context_outside_calibrated_support")
        cell = None
    else:
        cell = matching[0]
        if not cell["mature"] or cell["conservative_any_fill_probability"] is None:
            reasons.append("calibration_cell_immature")
    supported = not reasons
    return {
        "schema": SCHEMA, "experiment_id": calibration["experiment_id"], "model_sha": calibration["model_sha"],
        "calibration_sha256": calibration["calibration_sha256"], "context": context,
        "simulation_supported": supported, "live_execution_authorized": False,
        "conservative_any_fill_probability": cell["conservative_any_fill_probability"] if supported else None,
        "reason_codes": reasons, "state": "SIMULATION_SUPPORTED" if supported else "SIMULATION_UNSUPPORTED",
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationSupportError("input:unreadable") from exc
    return validate_calibration(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--context-json", required=True)
    args = parser.parse_args(argv)
    try:
        result = decide(_load(args.calibration), context=json.loads(args.context_json))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["simulation_supported"] else 1
    except (CalibrationSupportError, json.JSONDecodeError) as exc:
        print(f"v7_simulator_calibration_support: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
