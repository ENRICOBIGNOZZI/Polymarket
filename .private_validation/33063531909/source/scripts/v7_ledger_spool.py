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
from dataclasses import replace
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


def canonicalize_producer_event(event: LedgerEvent) -> LedgerEvent:
    """Map strategy-domain outcome labels into canonical execution semantics.

    The canonical ledger's ``side`` is deliberately BUY/SELL only. Graph/RV
    trades a token representing a YES/NO outcome and historically used that
    outcome label as ``side``. Preserve it explicitly as metadata while mapping
    the actual entry execution to BUY. No other strategy/domain label is
    silently accepted.
    """
    raw_side = str(event.side or "").upper()
    if event.strategy == "GRAPH_RV" and raw_side in {"YES", "NO"}:
        metadata = dict(event.metadata) if isinstance(event.metadata, dict) else {}
        existing = str(metadata.get("outcome_side") or "").upper()
        if existing and existing != raw_side:
            raise LedgerContractError("graph_rv:outcome_side_conflict")
        metadata["outcome_side"] = raw_side
        metadata["execution_side"] = "BUY"
        return replace(event, side="BUY", metadata=metadata)
    return event


def spool_event(run_root: Path, event: LedgerEvent) -> Path:
    event = canonicalize_producer_event(event)
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


def drain_spool(run_root: Path, *, model_sha: str, writer_id: str = "v7-canonical-ledger-router") -> dict[str, int]:
    root = Path(run_root)
    directory = spool_dir(root)
    ledger_path = canonical_ledger_path(root)
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    existing = _existing_record_ids(ledger_path)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Drain validated V7 strategy events into the canonical single-writer ledger")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--writer-id", default="v7-canonical-ledger-router")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    while True:
        result = drain_spool(args.run_root, model_sha=args.model_sha, writer_id=args.writer_id)
        print(json.dumps(result, sort_keys=True), flush=True)
        if not args.loop:
            return 0
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
