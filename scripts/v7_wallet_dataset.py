#!/usr/bin/env python3
"""Build a reproducible V7 wallet research dataset and manifest from JSONL."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from v7_wallet_intelligence import (
    MarketMapping, RawWalletFill, ResolvedOutcome, WalletError, historical_dataset,
)


def _load(path: Path, cls: type[Any]) -> list[Any]:
    rows: list[Any] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(cls(**json.loads(line)))
            except Exception as exc:
                raise WalletError(f"{path}:{line_number}:{exc}") from exc
    return rows


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False) + "\n")


def build_files(*, fills_path: Path, mappings_path: Path, outcomes_path: Path,
                dataset_path: Path, manifest_path: Path, as_of_ms: int,
                created_at_ms: int) -> None:
    rows, manifest = historical_dataset(
        _load(fills_path, RawWalletFill), _load(mappings_path, MarketMapping),
        _load(outcomes_path, ResolvedOutcome), as_of_ms=as_of_ms,
        created_at_ms=created_at_ms,
    )
    _write_jsonl(dataset_path, (asdict(row) for row in rows))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(asdict(manifest), sort_keys=True, indent=2,
                                        allow_nan=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fills", type=Path, required=True)
    parser.add_argument("--mappings", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--as-of-ms", type=int, required=True)
    parser.add_argument("--created-at-ms", type=int, required=True)
    args = parser.parse_args()
    build_files(fills_path=args.fills, mappings_path=args.mappings,
                outcomes_path=args.outcomes, dataset_path=args.dataset,
                manifest_path=args.manifest, as_of_ms=args.as_of_ms,
                created_at_ms=args.created_at_ms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_files", "main"]
