from __future__ import annotations

import csv
import json
import math
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Mapping


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
            encoded = ",".join(
                f'{key}="{_prom_escape(val)}"' for key, val in sorted(labels.items())
            )
            suffix = "{" + encoded + "}"
        self._lines.append(f"{name}{suffix} {_finite(float(value))}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [
                {str(key): str(value or "") for key, value in row.items() if key is not None}
                for row in csv.DictReader(handle)
                if row
            ]
    except (FileNotFoundError, OSError, csv.Error):
        return []


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class ExporterHandler(BaseHTTPRequestHandler):
    collector: Any

    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = self.collector.collect().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:
        return
