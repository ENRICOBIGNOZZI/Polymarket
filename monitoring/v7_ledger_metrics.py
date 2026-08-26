#!/usr/bin/env python3
"""Read-only aggregation for the canonical V7 execution ledger JSONL contract.

This reader intentionally mirrors the public phase-1 ledger semantics instead of
inventing monitoring-only event meanings. In particular, markouts are separate
``MARKOUT`` events; attaching markout payloads to fills is rejected so Grafana
cannot silently reinterpret a non-canonical side format as execution evidence.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

SHA40 = re.compile(r"^[0-9a-f]{40}$")
EVENT_TYPES = {
    "OPPORTUNITY",
    "CANDIDATE",
    "ORDER_SUBMITTED",
    "ORDER_STATE",
    "FILL",
    "MARKOUT",
    "POSITION_MARK",
    "EXIT",
    "FINAL",
}
MARKOUT_HORIZONS = ("1s", "10s", "45s", "60s", "300s")


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _blank() -> dict[str, Any]:
    return {
        "opportunities": 0,
        "candidates": 0,
        "orders_submitted": 0,
        "fills": 0,
        "complete_fills": 0,
        "partial_fills": 0,
        "markouts": 0,
        "exits": 0,
        "finals": 0,
        "unwinds": 0,
        "final_pnl": 0.0,
        "capital_duration_ms": 0.0,
        "markout_sum": {h: 0.0 for h in MARKOUT_HORIZONS},
        "markout_count": {h: 0 for h in MARKOUT_HORIZONS},
    }


def _canonical_markout(row: dict[str, Any], event_type: str) -> tuple[str, float] | None:
    raw = row.get("markouts")
    markouts = raw if isinstance(raw, dict) else {}
    if event_type != "MARKOUT":
        if markouts:
            raise ValueError("markouts_only_allowed_on_markout_event")
        return None
    if len(markouts) != 1:
        raise ValueError("markout_event_requires_one_horizon")
    horizon, raw_value = next(iter(markouts.items()))
    if horizon not in MARKOUT_HORIZONS:
        raise ValueError("unsupported_markout_horizon")
    value = _finite(raw_value)
    if value is None:
        raise ValueError("nonfinite_markout")
    return horizon, value


def summarize_ledger(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "present": False,
        "valid": False,
        "rows": 0,
        "invalid_rows": 0,
        "model_shas": [],
        "strategies": {},
        "total": _blank(),
    }
    path = Path(path)
    if not path.is_file():
        return result
    result["present"] = True
    shas: set[str] = set()
    strategies: dict[str, dict[str, Any]] = {}
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        result["invalid_rows"] = 1
        return result

    with handle:
        for line in handle:
            if not line.strip():
                continue
            result["rows"] += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                result["invalid_rows"] += 1
                continue
            if not isinstance(row, dict):
                result["invalid_rows"] += 1
                continue
            event_type = str(row.get("event_type") or "")
            strategy = str(row.get("strategy") or "").strip()
            model_sha = str(row.get("model_sha") or "").strip()
            if (
                event_type not in EVENT_TYPES
                or not strategy
                or not SHA40.fullmatch(model_sha)
                or row.get("paper_only") is not True
                or row.get("authenticated_execution") is not False
            ):
                result["invalid_rows"] += 1
                continue
            try:
                markout = _canonical_markout(row, event_type)
            except ValueError:
                result["invalid_rows"] += 1
                continue

            shas.add(model_sha)
            target = strategies.setdefault(strategy, _blank())
            for aggregate in (target, result["total"]):
                if event_type == "OPPORTUNITY":
                    aggregate["opportunities"] += 1
                elif event_type == "CANDIDATE":
                    aggregate["candidates"] += 1
                elif event_type == "ORDER_SUBMITTED":
                    aggregate["orders_submitted"] += 1
                elif event_type == "FILL":
                    aggregate["fills"] += 1
                    if row.get("complete") is True:
                        aggregate["complete_fills"] += 1
                    else:
                        aggregate["partial_fills"] += 1
                elif event_type == "MARKOUT":
                    aggregate["markouts"] += 1
                elif event_type == "EXIT":
                    aggregate["exits"] += 1
                elif event_type == "FINAL":
                    aggregate["finals"] += 1

                unwind = _finite(row.get("unwind_loss"))
                if unwind is not None and abs(unwind) > 0.0:
                    aggregate["unwinds"] += 1
                if event_type == "FINAL":
                    pnl = _finite(row.get("final_pnl"))
                    if pnl is not None:
                        aggregate["final_pnl"] += pnl
                    duration = _finite(row.get("capital_duration_ms"))
                    if duration is not None and duration >= 0.0:
                        aggregate["capital_duration_ms"] += duration
                if markout is not None:
                    horizon, value = markout
                    aggregate["markout_sum"][horizon] += value
                    aggregate["markout_count"][horizon] += 1

    result["model_shas"] = sorted(shas)
    result["strategies"] = strategies
    result["valid"] = (
        result["rows"] > 0
        and result["invalid_rows"] == 0
        and len(shas) == 1
    )
    return result
