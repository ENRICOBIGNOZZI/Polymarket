from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping


def _float(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _prom_escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _finite(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return format(value, ".12g")


class Metrics:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def _declare(self, name: str, help_text: str, metric_type: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {metric_type}")

    def sample(
        self,
        name: str,
        value: float | int,
        *,
        help_text: str,
        metric_type: str = "gauge",
        labels: Mapping[str, object] | None = None,
    ) -> None:
        self._declare(name, help_text, metric_type)
        suffix = ""
        if labels:
            encoded = ",".join(f'{key}="{_prom_escape(val)}"' for key, val in sorted(labels.items()))
            suffix = "{" + encoded + "}"
        self._lines.append(f"{name}{suffix} {_finite(float(value))}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            obj = json.load(handle)
        return obj if isinstance(obj, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
