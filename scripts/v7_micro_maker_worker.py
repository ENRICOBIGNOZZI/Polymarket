#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

import v7_micro_maker_worker_eventtime_core as core

MAX_MARKOUT_LABEL_DELAY_SECONDS = 15


def _run_dir(argv: list[str]) -> Path | None:
    try:
        index = argv.index("--run-dir")
        return Path(argv[index + 1])
    except (ValueError, IndexError):
        return None


def _finite(value: Any, default: float = float("nan")) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out


def _atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in rows])
    os.replace(tmp, path)


def filter_late_markouts(run_dir: Path, max_label_delay_seconds: int = MAX_MARKOUT_LABEL_DELAY_SECONDS) -> dict[str, int]:
    """Fail closed on markouts observed materially after their nominal horizon.

    The event-time core records the actual observation age. A worker outage must
    never turn a 90-second-old book into a nominal 45-second label. Late labels
    are removed from promotion evidence rather than backfilled with current data.
    """
    path = run_dir / "maker_markouts.csv"
    if not path.exists():
        return {"kept": 0, "rejected_late": 0, "rejected_invalid": 0}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except OSError:
        return {"kept": 0, "rejected_late": 0, "rejected_invalid": 0}
    if not fields:
        return {"kept": 0, "rejected_late": 0, "rejected_invalid": len(rows)}

    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    late = invalid = 0
    bound = max(0, int(max_label_delay_seconds))
    for row in rows:
        horizon = _finite(row.get("horizon_seconds"))
        age = _finite(row.get("observed_age_seconds"))
        if horizon <= 0 or age < horizon or not (horizon < float("inf") and age < float("inf")):
            invalid += 1
            rejected.append({**row, "label_delay_seconds": "", "reject_reason": "invalid_markout_clock"})
            continue
        delay = age - horizon
        if delay > bound + 1e-12:
            late += 1
            rejected.append({**row, "label_delay_seconds": f"{delay:.12g}", "reject_reason": "late_markout_label"})
            continue
        kept.append(row)

    _atomic_csv(path, fields, kept)
    if rejected:
        reject_path = run_dir / "maker_markout_rejections.csv"
        reject_fields = fields + ["label_delay_seconds", "reject_reason"]
        existing: list[dict[str, Any]] = []
        if reject_path.exists():
            try:
                with reject_path.open(newline="", encoding="utf-8") as handle:
                    existing = [dict(row) for row in csv.DictReader(handle)]
            except OSError:
                existing = []
        _atomic_csv(reject_path, reject_fields, existing + rejected)
    return {"kept": len(kept), "rejected_late": late, "rejected_invalid": invalid}


def _annotate(run_dir: Path, stats: dict[str, int]) -> None:
    for name in ("status.json", "state.json"):
        path = run_dir / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        value["markout_label_contract"] = "event_time_horizon_with_bounded_observation_delay"
        value["markout_max_label_delay_seconds"] = MAX_MARKOUT_LABEL_DELAY_SECONDS
        value["markout_rows_kept"] = int(stats.get("kept", 0))
        value["markout_rows_rejected_late"] = int(stats.get("rejected_late", 0))
        value["markout_rows_rejected_invalid"] = int(stats.get("rejected_invalid", 0))
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)


for _name in dir(core):
    if not _name.startswith("__") and _name != "main":
        globals()[_name] = getattr(core, _name)


def main() -> int:
    run_dir = _run_dir(list(sys.argv))
    rc = core.main()
    if run_dir is not None:
        stats = filter_late_markouts(run_dir)
        _annotate(run_dir, stats)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
