from __future__ import annotations

from pathlib import Path

import exporter_latest as latest

_original_health = latest.v6_runtime_data_health


def _v7_layout_health(root: Path, *, proxy_port: int | None = None):
    execution = Path(root) / "execution"
    return _original_health(execution if execution.is_dir() else Path(root), proxy_port=proxy_port)


latest.v6_runtime_data_health = _v7_layout_health


if __name__ == "__main__":
    raise SystemExit(latest.main())
