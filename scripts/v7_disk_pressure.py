#!/usr/bin/env python3
"""Fail-closed local disk headroom gate for V7 PAPER new risk.

This module has no execution authority. It only reports whether a caller may
continue considering a new PAPER risk action. CANCEL/WITHDRAW paths must remain
available independently.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "v7_data_retention.json"


def _policy() -> tuple[int | None, str | None]:
    try:
        value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        disk = value.get("disk") if isinstance(value, dict) else None
        threshold = disk.get("emergency_cleanup_free_bytes") if isinstance(disk, dict) else None
        if type(threshold) is not int or threshold <= 0:
            raise ValueError("positive emergency_cleanup_free_bytes required")
        return threshold, None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, f"DISK_POLICY_INVALID:{type(exc).__name__}:{exc}"


NEW_RISK_MIN_FREE_BYTES, POLICY_ERROR = _policy()


def disk_pressure_status(run_root: Path) -> dict[str, Any]:
    root = Path(run_root)
    marker = root / "control" / "DISK_PRESSURE"
    if POLICY_ERROR is not None or NEW_RISK_MIN_FREE_BYTES is None:
        return {
            "active": True, "reason": POLICY_ERROR or "DISK_POLICY_INVALID",
            "free_bytes": None, "threshold_bytes": NEW_RISK_MIN_FREE_BYTES,
            "marker_present": marker.exists(),
        }
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        return {
            "active": True, "reason": f"DISK_USAGE_UNAVAILABLE:{type(exc).__name__}",
            "free_bytes": None, "threshold_bytes": NEW_RISK_MIN_FREE_BYTES,
            "marker_present": marker.exists(),
        }
    marker_present = marker.exists()
    live_scope = root.name == "paper_v7_live" and root.parent.name == "runs"
    low = live_scope and usage.free <= NEW_RISK_MIN_FREE_BYTES
    return {
        "active": marker_present or low,
        "reason": "DISK_PRESSURE_MARKER" if marker_present else
                  "DISK_HEADROOM_BELOW_SAFE_THRESHOLD" if low else
                  "NOT_LIVE_SCOPE" if not live_scope else "HEADROOM_OK",
        "free_bytes": int(usage.free),
        "threshold_bytes": NEW_RISK_MIN_FREE_BYTES,
        "marker_present": marker_present,
    }
