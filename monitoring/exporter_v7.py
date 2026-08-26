#!/usr/bin/env python3
"""Native Prometheus exporter for the canonical V7 PAPER runtime.

Reads only V7 run-root contracts. Missing/stale causal state fails `/healthz`
closed; `/metrics` stays readable for diagnosis. The canonical execution ledger
is consumed read-only when present and never replaced by side-file assumptions.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from v7_ledger_metrics import summarize_ledger


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any, default: int = 0) -> int:
    return int(_number(value, float(default)))


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _age(now: int, value: Any) -> float:
    ts = _number(value, 0.0)
    return math.inf if ts <= 0.0 else max(0.0, float(now) - ts)


def _git_head(repository_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _safe_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(name: str, value: Any, labels: dict[str, Any] | None = None) -> str:
    numeric = _number(value, 0.0)
    if labels:
        encoded = ",".join(f'{key}="{_safe_label(item)}"' for key, item in labels.items())
        return f"{name}{{{encoded}}} {numeric:.12g}"
    return f"{name} {numeric:.12g}"


def collect_snapshot(run_root: Path, repository_root: Path | None = None, *, now: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    run_root = run_root.resolve()
    repository_root = (repository_root or Path(".")).resolve()
    supervisor = _json(run_root / "v7_supervisor.json")
    execution_supervisor = _json(run_root / "execution" / "v7_execution_supervisor.json")
    runtime = _json(run_root / "execution" / "runtime_status.json") or _json(run_root / "runtime_status.json")
    proxy = _json(run_root / "execution" / "market_proxy_status.json")
    evidence = _json(run_root / "execution" / "v7_execution_evidence.json")
    shadow = _json(run_root / "shadow" / "scheduler_status.json")
    allocator = _json(run_root / "execution" / "allocator_status.json") or _json(run_root / "allocator_status.json")
    ledger = summarize_ledger(run_root / "ledger" / "execution.jsonl")

    evidence_models = evidence.get("models") if isinstance(evidence.get("models"), dict) else {}
    runtime_strategies = runtime.get("strategies") if isinstance(runtime.get("strategies"), dict) else {}
    strategies: dict[str, dict[str, Any]] = {}
    for name, row in runtime_strategies.items():
        if isinstance(row, dict):
            strategies[str(name)] = {
                "equity": _number(row.get("equity")),
                "pnl": _number(row.get("pnl")),
                "fills": _integer(row.get("fills")),
                "live_units": _integer(row.get("live_units")),
                "killed": bool(row.get("killed", False)),
                "signals": _integer(row.get("signals")),
                "best_edge": _number(row.get("best_edge")),
            }
    for name, row in evidence_models.items():
        if not isinstance(row, dict):
            continue
        strategies.setdefault(str(name), {}).update(
            {
                "orders_submitted": None if row.get("orders_submitted") is None else _integer(row.get("orders_submitted")),
                "evidence_fills": _integer(row.get("fills")),
                "fill_rate": None if row.get("fill_rate") is None else _number(row.get("fill_rate")),
                "net_pnl": _number(row.get("net_pnl")),
                "stressed_net_pnl": None if row.get("stressed_net_pnl") is None else _number(row.get("stressed_net_pnl")),
                "markout_observations": _integer(row.get("forward_markout_observations")),
                "mean_markout": None if row.get("mean_forward_markout") is None else _number(row.get("mean_forward_markout")),
                "paper_eligible": bool(row.get("paper_eligible", False)),
            }
        )
    for name, row in ledger.get("strategies", {}).items():
        if not isinstance(row, dict):
            continue
        strategies.setdefault(str(name), {}).update(
            {
                "ledger_opportunities": _integer(row.get("opportunities")),
                "ledger_orders": _integer(row.get("orders_submitted")),
                "ledger_fills": _integer(row.get("fills")),
                "ledger_complete_fills": _integer(row.get("complete_fills")),
                "ledger_partial_fills": _integer(row.get("partial_fills")),
                "ledger_unwinds": _integer(row.get("unwinds")),
                "ledger_final_pnl": _number(row.get("final_pnl")),
                "ledger_capital_hours": _number(row.get("capital_duration_ms")) / 3_600_000.0,
            }
        )

    shadow_jobs = shadow.get("last_started") if isinstance(shadow.get("last_started"), dict) else {}
    starting = _number(runtime.get("starting_capital"), 0.0)
    equity = _number(runtime.get("equity"), starting)
    realized = _number(runtime.get("realized_pnl"), 0.0)
    pnl = _number(runtime.get("pnl"), equity - starting)
    gross = _number(runtime.get("gross_exposure"), 0.0)
    return {
        "timestamp": now,
        "sha": _git_head(repository_root),
        "run_root": run_root.name,
        "supervisor": supervisor,
        "execution_supervisor": execution_supervisor,
        "runtime": runtime,
        "proxy": proxy,
        "evidence": evidence,
        "shadow": shadow,
        "allocator": allocator,
        "ledger": ledger,
        "strategies": strategies,
        "shadow_freshness": {str(name): _age(now, started) for name, started in shadow_jobs.items()},
        "ages": {
            "supervisor": _age(now, supervisor.get("timestamp")),
            "execution_supervisor": _age(now, execution_supervisor.get("timestamp")),
            "runtime": _age(now, runtime.get("timestamp")),
            "proxy": _age(now, proxy.get("timestamp")),
            "evidence": _age(now, evidence.get("timestamp")),
            "shadow": _age(now, shadow.get("timestamp")),
        },
        "economics": {
            "starting_capital": starting,
            "cash": _number(runtime.get("cash"), 0.0),
            "equity": equity,
            "pnl": pnl,
            "realized_pnl": realized,
            "unrealized_executable_pnl": pnl - realized,
            "drawdown": _number(runtime.get("drawdown"), 0.0),
            "gross_exposure": gross,
            "capital_utilization": gross / starting if starting > 0.0 else 0.0,
            "live_units": _integer(runtime.get("live_units")),
            "killed": bool(runtime.get("killed", False)),
        },
    }


def health_reasons(snapshot: dict[str, Any], *, max_runtime_age: int = 180, max_supervisor_age: int = 30) -> list[str]:
    reasons: list[str] = []
    runtime = snapshot["runtime"]
    supervisor = snapshot["supervisor"]
    execution = snapshot["execution_supervisor"]
    proxy = snapshot["proxy"]
    evidence = snapshot["evidence"]
    ages = snapshot["ages"]
    ledger = snapshot["ledger"]
    if runtime.get("version") != 7:
        reasons.append("runtime_version_not_v7")
    if runtime.get("paper_only") is not True:
        reasons.append("runtime_not_paper_only")
    if runtime.get("authenticated_execution") is not False:
        reasons.append("authenticated_execution_not_disabled")
    if supervisor.get("execution_alive") is not True:
        reasons.append("execution_not_alive")
    if supervisor.get("shadow_alive") is not True:
        reasons.append("shadow_not_alive")
    if execution.get("paper_only") is not True:
        reasons.append("execution_supervisor_not_paper_only")
    if proxy.get("schema") != "polymarket_v7_market_proxy_status_v1":
        reasons.append("market_proxy_schema_not_v7")
    if proxy.get("paper_only") is not True:
        reasons.append("market_proxy_not_paper_only")
    if _integer(proxy.get("markets")) <= 0:
        reasons.append("market_proxy_empty")
    if evidence.get("paper_only") is not True:
        reasons.append("execution_evidence_missing_or_not_paper_only")
    if ledger.get("present") and not ledger.get("valid"):
        reasons.append("canonical_ledger_invalid_or_mixed_sha")
    for key in ("runtime", "execution_supervisor", "proxy", "evidence"):
        if not math.isfinite(float(ages[key])) or float(ages[key]) > max_runtime_age:
            reasons.append(f"{key}_stale")
    if not math.isfinite(float(ages["supervisor"])) or float(ages["supervisor"]) > max_supervisor_age:
        reasons.append("supervisor_stale")
    if snapshot["economics"]["drawdown"] > 0.15 + 1e-12:
        reasons.append("drawdown_limit_breached")
    return sorted(set(reasons))


def render_prometheus(snapshot: dict[str, Any]) -> str:
    runtime = snapshot["runtime"]
    proxy = snapshot["proxy"]
    evidence = snapshot["evidence"]
    ledger = snapshot["ledger"]
    ledger_total = ledger.get("total") if isinstance(ledger.get("total"), dict) else {}
    economics = snapshot["economics"]
    ages = snapshot["ages"]
    supervisor = snapshot["supervisor"]
    evidence_summary = evidence.get("summary") if isinstance(evidence.get("summary"), dict) else {}
    labels = {"adapter": "v7_native", "run_root": snapshot["run_root"], "version": "v7"}
    lines = [
        "# TYPE polymarket_v7_runtime_info gauge",
        _metric("polymarket_v7_runtime_info", 1 if runtime.get("version") == 7 else 0),
        _metric("polymarket_runtime_info", 1, labels),
        _metric("polymarket_v7_deployed_sha_info", 1, {"sha": snapshot["sha"]}),
        _metric("polymarket_v7_execution_alive", 1 if supervisor.get("execution_alive") is True else 0),
        _metric("polymarket_v7_shadow_alive", 1 if supervisor.get("shadow_alive") is True else 0),
        _metric("polymarket_runtime_equity_usd", economics["equity"]),
        _metric("polymarket_runtime_pnl_usd", economics["pnl"]),
        _metric("polymarket_runtime_realized_pnl_usd", economics["realized_pnl"]),
        _metric("polymarket_runtime_unrealized_executable_pnl_usd", economics["unrealized_executable_pnl"]),
        _metric("polymarket_runtime_drawdown_ratio", economics["drawdown"]),
        _metric("polymarket_runtime_gross_exposure_usd", economics["gross_exposure"]),
        _metric("polymarket_runtime_capital_utilization_ratio", economics["capital_utilization"]),
        _metric("polymarket_runtime_live_units", economics["live_units"]),
        _metric("polymarket_runtime_killed", 1 if economics["killed"] else 0),
        _metric("polymarket_v7_market_proxy_markets", _integer(proxy.get("markets"))),
        _metric("polymarket_v7_market_proxy_upstream_ok", 1 if proxy.get("upstream_gamma_ok") is True else 0),
        _metric("polymarket_v7_market_proxy_failures", _integer(proxy.get("failures"))),
        _metric("polymarket_v7_paper_eligible_models", _integer(evidence_summary.get("paper_eligible_models"))),
        _metric("polymarket_v7_insufficient_evidence_models", _integer(evidence_summary.get("insufficient_evidence_models"))),
        _metric("polymarket_v7_ledger_present", 1 if ledger.get("present") else 0),
        _metric("polymarket_v7_ledger_valid", 1 if ledger.get("valid") else 0),
        _metric("polymarket_v7_ledger_rows", _integer(ledger.get("rows"))),
        _metric("polymarket_v7_ledger_invalid_rows", _integer(ledger.get("invalid_rows"))),
        _metric("polymarket_v7_ledger_model_sha_count", len(ledger.get("model_shas") or [])),
        _metric("polymarket_execution_opportunities", _integer(ledger_total.get("opportunities"))),
        _metric("polymarket_execution_orders_submitted", _integer(ledger_total.get("orders_submitted"))),
        _metric("polymarket_execution_fills", _integer(ledger_total.get("fills"))),
        _metric("polymarket_execution_complete_fills", _integer(ledger_total.get("complete_fills"))),
        _metric("polymarket_execution_partial_fills", _integer(ledger_total.get("partial_fills"))),
        _metric("polymarket_execution_unwinds", _integer(ledger_total.get("unwinds"))),
        _metric("polymarket_execution_final_pnl_usd", _number(ledger_total.get("final_pnl"))),
        _metric("polymarket_execution_capital_hours", _number(ledger_total.get("capital_duration_ms")) / 3_600_000.0),
    ]
    for name, age in ages.items():
        lines.append(_metric("polymarket_v7_state_age_seconds", 0 if not math.isfinite(float(age)) else age, {"surface": name}))
        lines.append(_metric("polymarket_v7_state_present", 1 if math.isfinite(float(age)) else 0, {"surface": name}))
    for horizon, total in (ledger_total.get("markout_sum") or {}).items():
        count = _integer((ledger_total.get("markout_count") or {}).get(horizon))
        if count:
            lines.append(_metric("polymarket_execution_mean_markout", _number(total) / count, {"horizon": horizon}))
            lines.append(_metric("polymarket_execution_markout_observations", count, {"horizon": horizon}))

    for strategy, row in sorted(snapshot["strategies"].items()):
        strategy_label = {"strategy": strategy}
        mapping = (
            ("equity", "polymarket_strategy_equity_usd"),
            ("pnl", "polymarket_strategy_pnl_usd"),
            ("fills", "polymarket_strategy_fills"),
            ("live_units", "polymarket_strategy_live_units"),
            ("signals", "polymarket_strategy_opportunities"),
            ("best_edge", "polymarket_strategy_best_edge"),
            ("orders_submitted", "polymarket_strategy_orders_submitted"),
            ("evidence_fills", "polymarket_strategy_evidence_fills"),
            ("fill_rate", "polymarket_strategy_fill_rate"),
            ("net_pnl", "polymarket_strategy_evidence_net_pnl_usd"),
            ("stressed_net_pnl", "polymarket_strategy_stressed_net_pnl_usd"),
            ("markout_observations", "polymarket_strategy_markout_observations"),
            ("mean_markout", "polymarket_strategy_mean_markout"),
            ("ledger_opportunities", "polymarket_strategy_ledger_opportunities"),
            ("ledger_orders", "polymarket_strategy_ledger_orders_submitted"),
            ("ledger_fills", "polymarket_strategy_ledger_fills"),
            ("ledger_complete_fills", "polymarket_strategy_complete_fills"),
            ("ledger_partial_fills", "polymarket_strategy_partial_fills"),
            ("ledger_unwinds", "polymarket_strategy_unwinds"),
            ("ledger_final_pnl", "polymarket_strategy_final_pnl_usd"),
            ("ledger_capital_hours", "polymarket_strategy_capital_hours"),
        )
        for key, metric_name in mapping:
            if key in row and row[key] is not None:
                lines.append(_metric(metric_name, row[key], strategy_label))
        if "killed" in row:
            lines.append(_metric("polymarket_strategy_killed", 1 if row["killed"] else 0, strategy_label))
        if "paper_eligible" in row:
            lines.append(_metric("polymarket_strategy_paper_eligible", 1 if row["paper_eligible"] else 0, strategy_label))
    for job, age in sorted(snapshot["shadow_freshness"].items()):
        lines.append(_metric("polymarket_v7_shadow_job_age_seconds", age, {"job": job}))
    return "\n".join(lines) + "\n"


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
