#!/usr/bin/env python3
"""Repeatable, immutable capacity snapshots for LEAD_LAG_TAKER_V1.

Each invocation replays all currently settled PAPER evidence, writes one
content-addressed timestamped snapshot, updates stable ``latest`` outputs, and
rebuilds a compact longitudinal history. Identical evidence is idempotent.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from v7_lead_lag_capacity_ladder import build_ladder_report, write_ladder_scenario_csv
from v7_lead_lag_capacity_replay import (
    DEFAULT_NOTIONAL_GRID,
    DEFAULT_SHARE_GRID,
    build_report,
    collect_candidate_markets,
    collect_trades,
    parse_grid,
    write_scenario_csv,
    write_trade_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ROOT = REPO_ROOT / "runs/paper_v7_live"
DEFAULT_OUTPUT_NAME = "lead_lag_capacity_v1"
HISTORY_FIELDS = (
    "snapshot_id", "created_at_utc", "evidence_fingerprint", "runtime_sha",
    "candidate_markets", "settled_markets", "observed_pnl_usd",
    "median_best_ask_shares", "median_best_ask_notional_usd",
    "ladder_evidence_markets", "share_5_participation", "share_25_participation",
    "share_50_participation", "share_100_participation", "share_250_participation",
    "share_500_participation", "share_1000_participation",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
    }

def evidence_fingerprint(sources: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [{"bytes": row["bytes"], "sha256": row["sha256"]} for row in sources],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + f".tmp.{os.getpid()}")
    shutil.copyfile(source, tmp)
    os.replace(tmp, destination)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}

def runtime_identity(run_root: Path) -> dict[str, Any]:
    raw = read_json(run_root / "control/runtime_status.json")
    return {
        "model_sha": raw.get("model_sha"),
        "run_id": raw.get("run_id"),
        "server_id": raw.get("server_id"),
        "state": raw.get("state"),
        "paper_only": raw.get("paper_only"),
        "authenticated_execution": raw.get("authenticated_execution"),
        "real_order_submission": raw.get("real_order_submission"),
    }


def enrich_report_with_ladder(
    report: dict[str, Any], ledger: Path,
    share_grid: tuple[float, ...], notional_grid: tuple[float, ...],
) -> None:
    ladder = build_ladder_report(
        ledger,
        share_grid=share_grid,
        notional_grid=notional_grid,
    )
    report["full_ladder_capacity"] = ladder
    boundary = report.setdefault("evidence_boundary", {})
    boundary["full_book_sweep_replay_available"] = bool(ladder.get("available"))
    boundary["full_book_sweep_evidence_markets"] = int(ladder.get("evidence_markets") or 0)


def scenario_lookup(report: dict[str, Any], target: float) -> dict[str, Any] | None:
    for row in report.get("strict_fixed_share_grid") or []:
        if abs(float(row.get("target") or 0) - target) <= 1e-12:
            return row
    return None

def history_row(manifest: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "snapshot_id": manifest["snapshot_id"],
        "created_at_utc": manifest["created_at_utc"],
        "evidence_fingerprint": manifest["evidence_fingerprint"],
        "runtime_sha": (manifest.get("runtime") or {}).get("model_sha"),
        "candidate_markets": report.get("candidate_markets"),
        "settled_markets": report.get("settled_reference_markets"),
        "observed_pnl_usd": (report.get("observed_forward_test") or {}).get("realized_pnl_usd"),
        "median_best_ask_shares": (((report.get("same_price_capacity") or {}).get("top_visible_shares") or {}).get("median")),
        "median_best_ask_notional_usd": (((report.get("same_price_capacity") or {}).get("top_visible_notional_usd") or {}).get("median")),
        "ladder_evidence_markets": ((report.get("full_ladder_capacity") or {}).get("evidence_markets") or 0),
    }
    for target in DEFAULT_SHARE_GRID:
        scenario = scenario_lookup(report, target) or {}
        row[f"share_{int(target)}_participation"] = scenario.get("participation_rate")
    return row


def numeric_delta(current: Any, previous: Any) -> float | None:
    try:
        if current is None or previous is None:
            return None
        return float(current) - float(previous)
    except (TypeError, ValueError, OverflowError):
        return None


def compare_reports(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return {"previous_snapshot": None, "first_snapshot": True}
    curr_pnl = (current.get("observed_forward_test") or {}).get("realized_pnl_usd")
    prev_pnl = (previous.get("observed_forward_test") or {}).get("realized_pnl_usd")
    return {
        "first_snapshot": False,
        "candidate_markets_delta": numeric_delta(current.get("candidate_markets"), previous.get("candidate_markets")),
        "settled_markets_delta": numeric_delta(current.get("settled_reference_markets"), previous.get("settled_reference_markets")),
        "observed_pnl_usd_delta": numeric_delta(curr_pnl, prev_pnl),
        "median_best_ask_shares_delta": numeric_delta(
            (((current.get("same_price_capacity") or {}).get("top_visible_shares") or {}).get("median")),
            (((previous.get("same_price_capacity") or {}).get("top_visible_shares") or {}).get("median")),
        ),
        "ladder_evidence_markets_delta": numeric_delta(
            ((current.get("full_ladder_capacity") or {}).get("evidence_markets") or 0),
            ((previous.get("full_ladder_capacity") or {}).get("evidence_markets") or 0),
        ),
    }

def list_snapshots(history_root: Path) -> list[tuple[dict[str, Any], dict[str, Any], Path]]:
    rows: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
    if not history_root.exists():
        return rows
    for directory in sorted(path for path in history_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        manifest = read_json(directory / "manifest.json")
        report = read_json(directory / "report.json")
        if manifest.get("schema") != "polymarket_v7_lead_lag_capacity_snapshot_v1" or not report:
            continue
        rows.append((manifest, report, directory))
    rows.sort(key=lambda item: (int(item[0].get("created_at_ms") or 0), item[0].get("snapshot_id") or ""))
    return rows


def find_fingerprint(history_root: Path, fingerprint: str) -> tuple[dict[str, Any], Path] | None:
    for manifest, _, directory in list_snapshots(history_root):
        if manifest.get("evidence_fingerprint") == fingerprint:
            return manifest, directory
    return None


def write_history_csv(output_root: Path, snapshots: list[tuple[dict[str, Any], dict[str, Any], Path]]) -> None:
    destination = output_root / "history.csv"
    tmp = destination.with_name(destination.name + f".tmp.{os.getpid()}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        for manifest, report, _ in snapshots:
            writer.writerow(history_row(manifest, report))
    os.replace(tmp, destination)


def latest_snapshot(history_root: Path) -> tuple[dict[str, Any], dict[str, Any], Path] | None:
    rows = list_snapshots(history_root)
    return rows[-1] if rows else None

def materialize_latest(output_root: Path, snapshot_dir: Path) -> None:
    for name in ("report.json", "scenarios.csv", "ladder_scenarios.csv", "trades.csv", "manifest.json", "comparison.json"):
        source = snapshot_dir / name
        if source.exists():
            target = output_root / ("latest_manifest.json" if name == "manifest.json" else name)
            atomic_copy(source, target)
    pointer = {
        "schema": "polymarket_v7_lead_lag_capacity_latest_v1",
        "snapshot_id": snapshot_dir.name,
        "snapshot_path": str(snapshot_dir.resolve()),
    }
    atomic_text(output_root / "latest.json", json.dumps(pointer, indent=2, sort_keys=True) + "\n")


def build_snapshot(
    *, run_root: Path, output_root: Path, share_grid: tuple[float, ...],
    notional_grid: tuple[float, ...], bootstrap_draws: int, seed: int,
    label: str | None, force: bool,
) -> dict[str, Any]:
    ledger = run_root / "ledger/execution.jsonl"
    events = run_root / "research/lead_lag_taker_v1/events.jsonl"
    if not ledger.is_file() or not events.is_file():
        raise FileNotFoundError("capacity history requires canonical ledger and lead-lag event journal")
    sources = [source_identity(ledger), source_identity(events)]
    fingerprint = evidence_fingerprint(sources)
    history_root = output_root / "history"
    history_root.mkdir(parents=True, exist_ok=True)
    existing = find_fingerprint(history_root, fingerprint)
    if existing and not force:
        manifest, directory = existing
        materialize_latest(output_root, directory)
        write_history_csv(output_root, list_snapshots(history_root))
        return {"state": "UNCHANGED_EVIDENCE", "snapshot_id": manifest["snapshot_id"], "path": str(directory)}

    trades = collect_trades(ledger)
    candidate_evidence = collect_candidate_markets(events)
    report = build_report(
        trades, share_grid=share_grid, notional_grid=notional_grid,
        bootstrap_draws=bootstrap_draws, seed=seed,
        candidate_evidence=candidate_evidence,
    )
    enrich_report_with_ladder(report, ledger, share_grid, notional_grid)
    previous = latest_snapshot(history_root)
    previous_report = previous[1] if previous else None
    comparison = compare_reports(report, previous_report)
    created_ms = time.time_ns() // 1_000_000
    created_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_id = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_id += "-" + fingerprint[:12]
    if force and (history_root / snapshot_id).exists():
        snapshot_id += f"-{created_ms % 1000:03d}"
    manifest = {
        "schema": "polymarket_v7_lead_lag_capacity_snapshot_v1",
        "snapshot_id": snapshot_id,
        "created_at_ms": created_ms,
        "created_at_utc": created_at,
        "label": label,
        "paper_only": True,
        "real_order_submission": False,
        "evidence_fingerprint": fingerprint,
        "sources": sources,
        "runtime": runtime_identity(run_root),
        "parameters": {
            "shares_grid": list(share_grid),
            "notional_grid": list(notional_grid),
            "bootstrap_draws": bootstrap_draws,
            "seed": seed,
        },
        "previous_snapshot_id": previous[0].get("snapshot_id") if previous else None,
    }
    report["snapshot"] = {
        "snapshot_id": snapshot_id,
        "created_at_utc": created_at,
        "evidence_fingerprint": fingerprint,
        "previous_snapshot_id": manifest["previous_snapshot_id"],
    }
    comparison["previous_snapshot"] = manifest["previous_snapshot_id"]
    tmp_dir = Path(tempfile.mkdtemp(prefix=".capacity-snapshot-", dir=history_root))
    try:
        (tmp_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (tmp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (tmp_dir / "comparison.json").write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_scenario_csv(tmp_dir / "scenarios.csv", report)
        write_ladder_scenario_csv(tmp_dir / "ladder_scenarios.csv", report["full_ladder_capacity"])
        write_trade_csv(tmp_dir / "trades.csv", report)
        snapshot_dir = history_root / snapshot_id
        os.replace(tmp_dir, snapshot_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    snapshots = list_snapshots(history_root)
    materialize_latest(output_root, snapshot_dir)
    write_history_csv(output_root, snapshots)
    return {
        "state": "SNAPSHOT_CREATED",
        "snapshot_id": snapshot_id,
        "path": str(snapshot_dir),
        "candidate_markets": report.get("candidate_markets"),
        "settled_markets": report.get("settled_reference_markets"),
        "observed_pnl_usd": (report.get("observed_forward_test") or {}).get("realized_pnl_usd"),
        "ladder_evidence_markets": (report.get("full_ladder_capacity") or {}).get("evidence_markets", 0),
        "comparison": comparison,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--shares-grid", type=parse_grid, default=DEFAULT_SHARE_GRID)
    parser.add_argument("--notional-grid", type=parse_grid, default=DEFAULT_NOTIONAL_GRID)
    parser.add_argument("--bootstrap-draws", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--label")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.bootstrap_draws < 0:
        parser.error("--bootstrap-draws must be non-negative")
    run_root = args.run_root.resolve()
    output_root = (args.output_root or (run_root / "research" / DEFAULT_OUTPUT_NAME)).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / ".capacity.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        result = build_snapshot(
            run_root=run_root,
            output_root=output_root,
            share_grid=tuple(args.shares_grid),
            notional_grid=tuple(args.notional_grid),
            bootstrap_draws=args.bootstrap_draws,
            seed=args.seed,
            label=args.label,
            force=args.force,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
