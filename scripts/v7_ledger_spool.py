#!/usr/bin/env python3
"""Single-writer transport for the canonical V7 execution ledger.

Strategy workers may create fully validated ``LedgerEvent`` records in an atomic
spool, but only this module drains them into ``ledger/execution.jsonl``. The
spool is transport, not an alternate evidence store: canonical research reads
only the append-only execution ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Iterable

from v7_execution_ledger import (
    CanonicalLedgerWriter,
    LedgerContractError,
    LedgerEvent,
    canonical_ledger_path,
)


def spool_dir(run_root: Path) -> Path:
    return Path(run_root) / "ledger" / "spool"


def spool_event(run_root: Path, event: LedgerEvent) -> Path:
    event.validate()
    directory = spool_dir(run_root)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{event.recorded_ts_ms:013d}.{event.record_id}.json"
    target = directory / name
    temporary = target.with_name(target.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(event.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def spool_events(run_root: Path, events: Iterable[LedgerEvent]) -> list[Path]:
    return [spool_event(run_root, event) for event in events]


def _existing_record_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.exists():
        return ids
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerContractError(f"canonical_line_{line_number}:invalid_json") from exc
            event = LedgerEvent.from_dict(raw)
            ids.add(event.record_id)
    return ids


def _drain_with_existing(
    run_root: Path,
    *,
    model_sha: str,
    existing: set[str],
    writer_id: str,
) -> dict[str, int]:
    """Drain one atomic batch using an already synchronized record-id cache.

    The cache is safe only for the canonical single-writer process. It removes
    the previous O(total-ledger-size) rescan from every 100ms transport cycle,
    while the ledger ownership lock is still acquired only for the actual append
    batch so an unclean process stop cannot leave a long-lived writer lock by
    design.
    """
    root = Path(run_root)
    directory = spool_dir(root)
    ledger_path = canonical_ledger_path(root)
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    appended = 0
    duplicates = 0
    rejected = 0
    events: list[tuple[Path, LedgerEvent]] = []
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            event = LedgerEvent.from_dict(raw)
            if event.model_sha != model_sha:
                raise LedgerContractError("spool:mixed_model_sha")
        except (OSError, json.JSONDecodeError, LedgerContractError):
            rejected += 1
            continue
        if event.record_id in existing:
            duplicates += 1
            path.unlink(missing_ok=True)
            continue
        events.append((path, event))

    if events:
        with CanonicalLedgerWriter(ledger_path, writer_id=writer_id, model_sha=model_sha) as writer:
            for path, event in events:
                writer.append(event)
                existing.add(event.record_id)
                path.unlink()
                appended += 1
    return {"queued": len(files), "appended": appended, "duplicates": duplicates, "rejected": rejected}


def drain_spool(
    run_root: Path,
    *,
    model_sha: str,
    writer_id: str = "v7-canonical-ledger-router",
) -> dict[str, int]:
    root = Path(run_root)
    existing = _existing_record_ids(canonical_ledger_path(root))
    return _drain_with_existing(
        root,
        model_sha=model_sha,
        existing=existing,
        writer_id=writer_id,
    )


def drain_spool_loop(
    run_root: Path,
    *,
    model_sha: str,
    writer_id: str = "v7-canonical-ledger-router",
    interval: float = 1.0,
) -> None:
    """Run the canonical router with O(new-events) steady-state work."""
    root = Path(run_root)
    existing = _existing_record_ids(canonical_ledger_path(root))
    while True:
        result = _drain_with_existing(
            root,
            model_sha=model_sha,
            existing=existing,
            writer_id=writer_id,
        )
        print(json.dumps(result, sort_keys=True), flush=True)
        time.sleep(max(0.1, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description="Drain validated V7 strategy events into the canonical single-writer ledger")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--writer-id", default="v7-canonical-ledger-router")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if args.loop:
        drain_spool_loop(
            args.run_root,
            model_sha=args.model_sha,
            writer_id=args.writer_id,
            interval=args.interval,
        )
        return 0
    result = drain_spool(args.run_root, model_sha=args.model_sha, writer_id=args.writer_id)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
