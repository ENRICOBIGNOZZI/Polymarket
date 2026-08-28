#!/usr/bin/env python3
"""Canonical V7 exporter with read-only maker-fillability diagnostics."""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import exporter_v7 as base
from v7_maker_fillability import summarize_maker_fillability

_FILLABILITY_CACHE_KEY: tuple[str, str, int] | None = None
_FILLABILITY_CACHE_VALUE: dict[str, Any] | None = None
_FILLABILITY_REFRESH_SECONDS = 30


def _fillability_report(run_root: Path, repository_root: Path, runtime_sha: str, now: int | None) -> dict[str, Any]:
    global _FILLABILITY_CACHE_KEY, _FILLABILITY_CACHE_VALUE
    clock_s = int(time.time()) if now is None else int(now)
    key = (str(run_root.resolve()), runtime_sha, clock_s // _FILLABILITY_REFRESH_SECONDS)
    if key == _FILLABILITY_CACHE_KEY and _FILLABILITY_CACHE_VALUE is not None:
        return _FILLABILITY_CACHE_VALUE
    report = summarize_maker_fillability(
        run_root.resolve() / "ledger" / "execution.jsonl",
        run_root.resolve() / "trade_tape.csv",
        repository_root / "config" / "v7_professional_market_maker.json",
        model_sha=runtime_sha or None,
        now_ms=clock_s * 1000,
    )
    _FILLABILITY_CACHE_KEY = key
    _FILLABILITY_CACHE_VALUE = report
    return report


def collect_snapshot(run_root: Path, repository_root: Path | None = None, *, now: int | None = None) -> dict[str, Any]:
    snapshot = base.collect_snapshot(run_root, repository_root, now=now)
    repository_root = (repository_root or Path(".")).resolve()
    runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    runtime_sha = str(runtime.get("model_sha") or "")
    snapshot["maker_fillability"] = _fillability_report(Path(run_root), repository_root, runtime_sha, now)
    return snapshot


def _append_fillability_metrics(lines: list[str], report: dict[str, Any]) -> None:
    funnel = report.get("funnel") if isinstance(report.get("funnel"), dict) else {}
    lines.extend([
        base._metric("polymarket_maker_fillability_present", 1 if report.get("exact_sha_ok") else 0),
        base._metric("polymarket_maker_fillability_orders", funnel.get("orders")),
        base._metric("polymarket_maker_fillability_orders_effective", funnel.get("orders_effective")),
        base._metric("polymarket_maker_fillability_orders_rested", funnel.get("orders_rested")),
        base._metric("polymarket_maker_fillability_trade_reachable", funnel.get("trade_reachable")),
        base._metric("polymarket_maker_fillability_partial_fills", funnel.get("partial_fills")),
        base._metric("polymarket_maker_fillability_full_fills", funnel.get("full_fills")),
        base._metric("polymarket_maker_fillability_cancelled_before_flow", funnel.get("cancelled_before_flow")),
        base._metric("polymarket_maker_fillability_priority_resets", funnel.get("priority_resets")),
        base._metric("polymarket_maker_fillability_root_cause_info", 1, {
            "root_cause": report.get("root_cause", "UNKNOWN"),
            "simulator_bug": report.get("simulator_bug_suspected", "UNKNOWN"),
            "next_experiment": report.get("next_experiment", "UNKNOWN"),
        }),
    ])
    for scenario, key in (("lower", "lower_queue_depleted"), ("expected", "expected_queue_depleted"), ("pessimistic", "pessimistic_queue_depleted")):
        lines.append(base._metric("polymarket_maker_fillability_queue_depleted", funnel.get(key), {"scenario": scenario}))
    for scenario, key in (("lower", "fill_opportunity_lower"), ("expected", "fill_opportunity_expected"), ("pessimistic", "fill_opportunity_pessimistic")):
        lines.append(base._metric("polymarket_maker_fillability_opportunities", funnel.get(key), {"scenario": scenario}))
    reasons = report.get("zero_fill_reasons") if isinstance(report.get("zero_fill_reasons"), dict) else {}
    for reason, value in sorted(reasons.items()):
        lines.append(base._metric("polymarket_maker_fillability_zero_fill_reason", value, {"reason": reason}))
    for row in report.get("actions") if isinstance(report.get("actions"), list) else []:
        if not isinstance(row, dict):
            continue
        labels = {"action": row.get("action", "UNKNOWN")}
        for field, metric in (
            ("orders", "polymarket_maker_fillability_action_orders"),
            ("trade_reachable", "polymarket_maker_fillability_action_trade_reachable"),
            ("pessimistic_queue_depleted", "polymarket_maker_fillability_action_pessimistic_queue_depleted"),
            ("fill_opportunities", "polymarket_maker_fillability_action_fill_opportunities"),
            ("filled_orders", "polymarket_maker_fillability_action_filled_orders"),
            ("mean_rest_ms", "polymarket_maker_fillability_action_mean_rest_ms"),
            ("mean_near_miss_ratio", "polymarket_maker_fillability_action_near_miss_ratio"),
            ("priority_resets", "polymarket_maker_fillability_action_priority_resets"),
        ):
            lines.append(base._metric(metric, row.get(field), labels))
    for row in report.get("markets") if isinstance(report.get("markets"), list) else []:
        if not isinstance(row, dict):
            continue
        labels = {"market": row.get("market_id", "UNKNOWN")}
        for field, metric in (
            ("orders", "polymarket_maker_fillability_market_orders"),
            ("trade_reachable", "polymarket_maker_fillability_market_trade_reachable"),
            ("fill_opportunities", "polymarket_maker_fillability_market_fill_opportunities"),
            ("filled_orders", "polymarket_maker_fillability_market_filled_orders"),
            ("mean_rest_ms", "polymarket_maker_fillability_market_mean_rest_ms"),
            ("mean_near_miss_ratio", "polymarket_maker_fillability_market_near_miss_ratio"),
            ("priority_resets", "polymarket_maker_fillability_market_priority_resets"),
        ):
            lines.append(base._metric(metric, row.get(field), labels))


def render_prometheus(snapshot: dict[str, Any]) -> str:
    payload = base.render_prometheus(snapshot).rstrip("\n").splitlines()
    report = snapshot.get("maker_fillability") if isinstance(snapshot.get("maker_fillability"), dict) else {}
    _append_fillability_metrics(payload, report)
    return "\n".join(payload) + "\n"


def health_reasons(snapshot: dict[str, Any], *, max_runtime_age: int = 180, max_supervisor_age: int = 30) -> list[str]:
    return base.health_reasons(snapshot, max_runtime_age=max_runtime_age, max_supervisor_age=max_supervisor_age)


class ExporterHandler(BaseHTTPRequestHandler):
    run_root = Path("runs/paper_v7_live")
    repository_root = Path(".")
    max_runtime_age = 180
    max_supervisor_age = 30

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        snapshot = collect_snapshot(self.run_root, self.repository_root)
        if self.path == "/metrics":
            payload = render_prometheus(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/healthz":
            reasons = health_reasons(snapshot, max_runtime_age=self.max_runtime_age, max_supervisor_age=self.max_supervisor_age)
            payload = (json.dumps({"ok": not reasons, "reasons": reasons}, sort_keys=True) + "\n").encode()
            self.send_response(200 if not reasons else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            payload = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--max-runtime-age", type=int, default=180)
    parser.add_argument("--max-supervisor-age", type=int, default=30)
    args = parser.parse_args()
    ExporterHandler.run_root = args.run_root
    ExporterHandler.repository_root = args.repository_root
    ExporterHandler.max_runtime_age = max(1, args.max_runtime_age)
    ExporterHandler.max_supervisor_age = max(1, args.max_supervisor_age)
    server = ThreadingHTTPServer((args.host, args.port), ExporterHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
