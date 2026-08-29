#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle) if row]
    except OSError:
        return []


def _safe_rel(value: Any, prefix: str) -> str:
    raw = str(value or "")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or not raw.startswith(prefix):
        raise ValueError(f"unsafe {prefix} path: {raw!r}")
    return raw


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.helped: set[str] = set()

    def sample(self, name: str, value: Any, *, help_text: str = "", labels: dict[str, Any] | None = None) -> None:
        numeric = _number(value)
        if help_text and name not in self.helped:
            self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} gauge")
            self.helped.add(name)
        suffix = ""
        if labels:
            joined = ",".join(f'{key}="{_label(val)}"' for key, val in sorted(labels.items()))
            suffix = "{" + joined + "}"
        self.lines.append(f"{name}{suffix} {numeric:.12g}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


class V7Collector:
    def __init__(self, repository_root: Path, manifest: Path, max_age_seconds: float = 180.0) -> None:
        self.repository_root = repository_root.resolve()
        self.manifest_path = manifest if manifest.is_absolute() else self.repository_root / manifest
        self.max_age_seconds = max_age_seconds

    def _manifest(self) -> tuple[dict[str, Any], Path, Path]:
        manifest = _read_json(self.manifest_path)
        if manifest.get("enabled") is not True:
            raise RuntimeError("live champion is disabled")
        if manifest.get("version") != 7:
            raise RuntimeError("live champion must be V7")
        if manifest.get("paper_only") is not True or manifest.get("authenticated_execution") is not False:
            raise RuntimeError("live champion violates PAPER/authenticated boundary")
        run_rel = _safe_rel(manifest.get("run_root"), "runs/")
        config_rel = _safe_rel(manifest.get("config"), "config/")
        _safe_rel(manifest.get("loop"), "scripts/")
        run_root = self.repository_root / run_rel
        config_path = self.repository_root / config_rel
        if not config_path.is_file():
            raise RuntimeError(f"champion config missing: {config_rel}")
        return manifest, run_root, config_path

    @staticmethod
    def _state_root(run_root: Path) -> Path:
        direct = run_root / "runtime_status.json"
        execution = run_root / "execution" / "runtime_status.json"
        if direct.is_file():
            return run_root
        if execution.is_file():
            return run_root / "execution"
        return run_root / "execution"

    def snapshot(self) -> tuple[dict[str, Any], str]:
        manifest, run_root, _ = self._manifest()
        state_root = self._state_root(run_root)
        status = _read_json(state_root / "runtime_status.json")
        allocator = _read_json(state_root / "allocator_status.json")
        strategies = _read_csv(state_root / "strategy_status.csv")
        supervisor = _read_json(run_root / "v7_supervisor.json")
        now = time.time()
        ts = _number(status.get("timestamp"))
        age = max(0.0, now - ts) if ts else float("inf")

        checks = {
            "status_present": bool(status),
            "runtime_v7": status.get("version") == 7,
            "paper_only": status.get("paper_only") is True and allocator.get("paper_only") is True,
            "authenticated_execution_disabled": status.get("authenticated_execution") is False and allocator.get("authenticated_execution") is False,
            "fresh": math.isfinite(age) and age <= self.max_age_seconds,
            "supervisor_execution_alive": supervisor.get("execution_alive") is True,
            "supervisor_shadow_alive": supervisor.get("shadow_alive") is True,
        }
        healthy = all(checks.values())

        metrics = Metrics()
        labels = {"version": "v7", "run_root": Path(str(manifest["run_root"])).name}
        metrics.sample("polymarket_v7_runtime_info", 1, help_text="Canonical V7 PAPER runtime identity.", labels=labels)
        metrics.sample("polymarket_v7_runtime_alive", 1 if healthy else 0, help_text="All V7 runtime safety and freshness checks pass.")
        metrics.sample("polymarket_v7_runtime_status_age_seconds", age if math.isfinite(age) else 1e12, help_text="Age of canonical V7 runtime status.")
        metrics.sample("polymarket_v7_runtime_equity_usd", status.get("equity"), help_text="Canonical V7 PAPER equity in USD.")
        metrics.sample("polymarket_v7_runtime_pnl_usd", status.get("pnl"), help_text="Canonical V7 total PAPER PnL in USD.")
        metrics.sample("polymarket_v7_runtime_realized_pnl_usd", status.get("realized_pnl"), help_text="Canonical V7 realized PAPER PnL in USD.")
        metrics.sample("polymarket_v7_runtime_drawdown_fraction", status.get("drawdown"), help_text="Canonical V7 drawdown fraction.")
        metrics.sample("polymarket_v7_runtime_gross_exposure_usd", status.get("gross_exposure"), help_text="Canonical V7 gross exposure in USD.")
        starting = max(_number(status.get("starting_capital")), 1e-12)
        metrics.sample("polymarket_v7_runtime_capital_utilization_fraction", _number(status.get("gross_exposure")) / starting, help_text="Gross exposure divided by starting capital.")
        metrics.sample("polymarket_v7_runtime_killed", 1 if status.get("killed") else 0, help_text="V7 hard-risk kill state.")
        metrics.sample("polymarket_v7_runtime_paper_only", 1 if status.get("paper_only") is True else 0, help_text="Runtime PAPER-only invariant.")
        metrics.sample("polymarket_v7_runtime_authenticated_execution", 1 if status.get("authenticated_execution") is True else 0, help_text="Authenticated execution invariant; must remain zero.")

        for row in strategies:
            strategy = str(row.get("name") or row.get("strategy") or "unknown")
            slabel = {"strategy": strategy}
            fills = _number(row.get("fills"))
            pnl = _number(row.get("pnl"))
            metrics.sample("polymarket_v7_strategy_equity_usd", row.get("equity"), help_text="V7 strategy PAPER equity.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_pnl_usd", pnl, help_text="V7 strategy PAPER PnL.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_realized_pnl_usd", row.get("realized_pnl"), help_text="V7 strategy realized PAPER PnL.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_fills_total", fills, help_text="V7 strategy PAPER fills.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_open_positions", row.get("open_positions"), help_text="V7 strategy open positions/live units.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_gross_exposure_usd", row.get("gross_exposure"), help_text="V7 strategy gross exposure.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_pnl_per_fill_usd", pnl / fills if fills > 0 else 0.0, help_text="V7 strategy PnL per PAPER fill.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_alive", row.get("alive"), help_text="V7 strategy liveness.", labels=slabel)
            metrics.sample("polymarket_v7_strategy_killed", row.get("killed"), help_text="V7 strategy kill state.", labels=slabel)

        strategy_details = status.get("strategies") if isinstance(status.get("strategies"), dict) else {}
        for strategy, detail in strategy_details.items():
            if not isinstance(detail, dict):
                continue
            slabel = {"strategy": strategy}
            if "signals" in detail:
                metrics.sample("polymarket_v7_strategy_opportunities_total", detail.get("signals"), help_text="V7 executable/positive candidates observed by strategy.", labels=slabel)
            if "best_edge" in detail:
                metrics.sample("polymarket_v7_strategy_best_edge", detail.get("best_edge"), help_text="Best current post-cost candidate edge by strategy.", labels=slabel)

        graph = status.get("graph_scan") if isinstance(status.get("graph_scan"), dict) else {}
        for key in ("opportunities", "candidate_count", "executable_candidates", "complete_fills", "partial_fills", "unwinds"):
            if key in graph:
                metrics.sample(f"polymarket_v7_graph_{key}", graph.get(key), help_text=f"V7 Graph/RV {key.replace('_', ' ')}.")

        ranking = _read_json(run_root / "shadow" / "cross_sectional_rank.json")
        rows = ranking.get("forward") if isinstance(ranking.get("forward"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            horizon = int(_number(row.get("horizon_minutes")))
            hlabel = {"horizon_minutes": horizon}
            metrics.sample("polymarket_v7_rank_completed_sections", row.get("completed_sections"), help_text="Forward ranking completed cross-sections.", labels=hlabel)
            metrics.sample("polymarket_v7_rank_mean_ic", row.get("mean_rank_ic"), help_text="Forward ranking mean rank IC.", labels=hlabel)
            metrics.sample("polymarket_v7_rank_tail_spread", row.get("mean_top_bottom_logit_spread"), help_text="Forward ranking top-bottom relative-logit spread.", labels=hlabel)

        pca = _read_json(run_root / "shadow" / "pca_stat_arb.json")
        if pca:
            metrics.sample("polymarket_v7_pca_selected_hypotheses", pca.get("bh_survivors", pca.get("selected_hypotheses")), help_text="V7 PCA statistically selected residual hypotheses.")
            metrics.sample("polymarket_v7_pca_shadow_candidates", pca.get("shadow_candidates", pca.get("candidate_count")), help_text="V7 PCA forward shadow candidates.")

        health = {
            "ok": healthy,
            "version": 7,
            "run_root": str(manifest["run_root"]),
            "status_age_seconds": age if math.isfinite(age) else None,
            "checks": checks,
        }
        return health, metrics.render()


class Handler(BaseHTTPRequestHandler):
    collector: V7Collector

    def _send(self, code: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        try:
            health, metrics = self.collector.snapshot()
        except Exception as exc:
            body = json.dumps({"ok": False, "error": str(exc)}, sort_keys=True) + "\n"
            if self.path == "/metrics":
                self._send(503, "# V7 exporter unavailable: " + str(exc).replace("\n", " ") + "\n", "text/plain; version=0.0.4")
            else:
                self._send(503, body, "application/json")
            return
        if self.path == "/healthz":
            self._send(200 if health["ok"] else 503, json.dumps(health, sort_keys=True) + "\n", "application/json")
        elif self.path == "/metrics":
            self._send(200 if health["ok"] else 503, metrics, "text/plain; version=0.0.4")
        else:
            self._send(404, "not found\n", "text/plain")

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Prometheus exporter for the canonical V7 PAPER runtime")
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=Path("config/live_champion.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--max-age-seconds", type=float, default=180.0)
    args = parser.parse_args()
    Handler.collector = V7Collector(args.repository_root, args.manifest, args.max_age_seconds)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
