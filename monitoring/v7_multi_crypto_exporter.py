#!/usr/bin/env python3
"""Lightweight Prometheus exporter for the V7 multi-crypto PAPER/SHADOW dashboard."""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from v7_ledger_metrics import summarize_ledger
from v7_multi_crypto_performance import (
    render_prometheus as render_multi_crypto_prometheus,
    render_shadow_prometheus,
    summarize_multi_crypto,
    summarize_shadow_runtime,
)


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def collect_snapshot(run_root: Path, repository_root: Path, shadow_run_root: Path | None, *, now_ns: int | None = None) -> dict[str, Any]:
    run_root = Path(run_root).resolve()
    repository_root = Path(repository_root).resolve()
    runtime = _json(run_root / "control/runtime_status.json")
    portfolio = _json(run_root / "control/portfolio_state.json")
    canonical_path = run_root / "canonical_economics.json"
    canonical = _json(canonical_path)
    ledger = summarize_ledger(run_root / "ledger/execution.jsonl")
    performance = summarize_multi_crypto(
        run_root,
        expected_sha=str(runtime.get("model_sha") or ""),
        portfolio=portfolio,
        canonical=canonical,
        global_coordinator=_json(run_root / "control/global_portfolio_coordinator.json"),
        crypto_registry=_json(repository_root / "config/v7_crypto_settlement_markets.json"),
        crypto_model_registry=_json(repository_root / "config/v7_crypto_settlement_model_registry.json"),
        ledger_valid=ledger.get("valid") is True,
        canonical_mtime_ms=(canonical_path.stat().st_mtime * 1000.0 if canonical_path.is_file() else None),
    )
    shadow = summarize_shadow_runtime(shadow_run_root, now_ns=now_ns)
    return {"runtime": runtime, "performance": performance, "shadow": shadow}


def render_prometheus(snapshot: dict[str, Any]) -> str:
    lines = render_multi_crypto_prometheus(snapshot.get("performance") or {})
    lines.extend(render_shadow_prometheus(snapshot.get("shadow") or {}))
    return "\n".join(lines) + "\n"


def health_reasons(snapshot: dict[str, Any], *, max_shadow_age: float = 30.0) -> list[str]:
    runtime = snapshot.get("runtime") or {}
    performance = snapshot.get("performance") or {}
    shadow = snapshot.get("shadow") or {}
    reasons: list[str] = []
    if runtime.get("paper_only") is not True: reasons.append("paper_only_not_true")
    if runtime.get("authenticated_execution") is not False: reasons.append("authenticated_execution_not_false")
    if runtime.get("real_order_submission") is not False: reasons.append("real_order_submission_not_false")
    if runtime.get("real_capital_at_risk") is not False: reasons.append("real_capital_at_risk_not_false")
    if not _pid_alive(runtime.get("pid")): reasons.append("paper_runtime_pid_not_alive")
    if performance.get("ledger_valid") is not True: reasons.append("canonical_ledger_invalid")
    if shadow.get("present") is True:
        if shadow.get("valid") is not True: reasons.append("shadow_status_invalid")
        if shadow.get("safe") is not True: reasons.append("shadow_not_safe")
        age = shadow.get("age_seconds")
        if not isinstance(age, (int, float)) or not math.isfinite(float(age)) or float(age) > max_shadow_age:
            reasons.append("shadow_status_stale")
    return sorted(set(reasons))


class SnapshotCache:
    def __init__(self, run_root: Path, repository_root: Path, shadow_run_root: Path | None, *, refresh_seconds: float = 2.0) -> None:
        self.run_root = Path(run_root)
        self.repository_root = Path(repository_root)
        self.shadow_run_root = Path(shadow_run_root) if shadow_run_root else None
        self.refresh_seconds = max(0.5, float(refresh_seconds))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._snapshot: dict[str, Any] | None = None
        self._metrics = b""
        self._performance = b"{}\n"
        self._shadow = b"{}\n"
        self._completed_monotonic = 0.0
        self._refresh_duration = 0.0
        self._errors = 0
        self._last_error = ""
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="v7-multi-crypto-exporter-refresh", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.refresh_seconds + 1.0))

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snapshot = collect_snapshot(self.run_root, self.repository_root, self.shadow_run_root, now_ns=time.time_ns())
                duration = time.monotonic() - started
                generated = time.time()
                metrics = render_prometheus(snapshot).rstrip() + (
                    f"\npolymarket_v7_exporter_snapshot_generated_unixtime {generated}\n"
                    f"polymarket_v7_exporter_snapshot_refresh_duration_seconds {duration}\n"
                    f"polymarket_v7_exporter_snapshot_refresh_errors_total {self._errors}\n"
                )
                with self._lock:
                    self._snapshot = snapshot
                    self._metrics = metrics.encode()
                    self._performance = (json.dumps(snapshot["performance"], sort_keys=True) + "\n").encode()
                    self._shadow = (json.dumps(snapshot["shadow"], sort_keys=True) + "\n").encode()
                    self._completed_monotonic = time.monotonic()
                    self._refresh_duration = duration
                    self._last_error = ""
                self._ready.set()
            except Exception as exc:
                with self._lock:
                    self._errors += 1
                    self._last_error = f"{type(exc).__name__}:{exc}"
            self._stop.wait(max(0.05, self.refresh_seconds - (time.monotonic() - started)))

    def read(self) -> dict[str, Any]:
        with self._lock:
            age = max(0.0, time.monotonic() - self._completed_monotonic) if self._completed_monotonic else math.inf
            return {
                "ready": self._snapshot is not None,
                "snapshot": self._snapshot,
                "metrics": self._metrics,
                "performance": self._performance,
                "shadow": self._shadow,
                "age_seconds": age,
                "refresh_duration_seconds": self._refresh_duration,
                "refresh_errors": self._errors,
                "last_error": self._last_error,
            }


class Handler(BaseHTTPRequestHandler):
    cache: SnapshotCache | None = None
    max_snapshot_age = 15.0
    max_shadow_age = 30.0

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        cached = self.cache.read() if self.cache else {"ready": False}
        status = 200
        content = "application/json"
        if not cached.get("ready"):
            status = 503
            payload = b'{"ok":false,"reasons":["exporter_snapshot_not_ready"]}\n'
        elif self.path == "/metrics":
            content = "text/plain; version=0.0.4"
            payload = cached["metrics"]
        elif self.path == "/multi-crypto-performance.json":
            payload = cached["performance"]
        elif self.path == "/multi-crypto-shadow.json":
            payload = cached["shadow"]
        elif self.path == "/healthz":
            reasons = health_reasons(cached["snapshot"], max_shadow_age=self.max_shadow_age)
            if float(cached.get("age_seconds", math.inf)) > self.max_snapshot_age:
                reasons.append("exporter_snapshot_stale")
            reasons = sorted(set(reasons))
            status = 200 if not reasons else 503
            payload = (json.dumps({"ok": not reasons, "reasons": reasons}, sort_keys=True) + "\n").encode()
        else:
            status = 404
            content = "text/plain"
            payload = b"not found\n"
        self.send_response(status)
        self.send_header("Content-Type", content + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--shadow-run-root", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9110)
    parser.add_argument("--snapshot-refresh-seconds", type=float, default=2.0)
    parser.add_argument("--max-snapshot-age", type=float, default=15.0)
    parser.add_argument("--max-shadow-age", type=float, default=30.0)
    args = parser.parse_args()
    cache = SnapshotCache(args.run_root, args.repository_root, args.shadow_run_root, refresh_seconds=args.snapshot_refresh_seconds)
    cache.start()
    cache.wait_ready(min(10.0, max(2.0, args.max_snapshot_age)))
    Handler.cache = cache
    Handler.max_snapshot_age = max(1.0, float(args.max_snapshot_age))
    Handler.max_shadow_age = max(1.0, float(args.max_shadow_age))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        cache.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
