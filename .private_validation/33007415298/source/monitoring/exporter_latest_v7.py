from __future__ import annotations

from pathlib import Path

import exporter_latest as latest

_original_health = latest.v6_runtime_data_health
_original_contract = latest._canonical_from_contract
_original_fallback = latest._canonical_fallback


def _execution_root(root: Path) -> Path:
    execution = Path(root) / "execution"
    return execution if execution.is_dir() else Path(root)


def _v7_layout_health(root: Path, *, proxy_port: int | None = None):
    return _original_health(_execution_root(root), proxy_port=proxy_port)


def _v7_canonical_from_contract(root: Path, now: float):
    return _original_contract(_execution_root(root), now)


def _v7_canonical_fallback(root: Path, config: Path, now: float):
    return _original_fallback(_execution_root(root), config, now)


latest.v6_runtime_data_health = _v7_layout_health
latest._canonical_from_contract = _v7_canonical_from_contract
latest._canonical_fallback = _v7_canonical_fallback


if __name__ == "__main__":
    raise SystemExit(latest.main())
