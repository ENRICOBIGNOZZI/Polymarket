#!/usr/bin/env python3
"""Prometheus exporter for the canonical V7 PAPER runtime.

Health is derived only from live V7 control-plane surfaces: the single runtime
PID/SHA, account portfolio guard, Graph/RV executor, public trade tape,
canonical execution ledger and canonical economics. Missing or stale causal
state fails `/healthz` closed; `/metrics` remains readable for diagnosis.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import socket
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
from v7_maker_microstructure import summarize_maker_microstructure


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


def _file_age(path: Path, now: int) -> float:
    try:
        return max(0.0, float(now) - path.stat().st_mtime)
    except OSError:
        return math.inf


def _git_head(repository_root: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repository_root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _pid_alive(value: Any) -> bool:
    pid = _integer(value, 0)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    return True


def _runtime_operations(run_root: Path, runtime: dict[str, Any], now: int) -> dict[str, Any]:
    supervisor = _json(run_root / "control" / "supervisor_status.json")
    retention = _json(run_root / "control" / "retention_status.json")
    lock_pid = 0
    try:
        lock_pid = int((run_root / "control" / "runtime.lock" / "pid").read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError, OverflowError):
        pass
    runtime_pid = _integer(runtime.get("pid"), 0)
    supervisor_pid = _integer(supervisor.get("supervisor_pid"), 0)
    child_pid = _integer(supervisor.get("child_pid"), 0)
    started_at = _integer(supervisor.get("started_at"), 0)
    disk = retention.get("disk") if isinstance(retention.get("disk"), dict) else {}
    if not disk:
        try:
            usage = shutil.disk_usage(run_root)
            disk = {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "free_ratio": usage.free / usage.total if usage.total else 0.0,
                "state": "unknown",
            }
        except OSError:
            disk = {}
    ledger_path = run_root / "ledger" / "execution.jsonl"
    ledger_parent = ledger_path.parent
    ledger_writable = ledger_parent.is_dir() and os.access(ledger_parent, os.W_OK)
    if ledger_path.exists():
        ledger_writable = ledger_writable and os.access(ledger_path, os.W_OK)
    return {
        "supervisor": supervisor,
        "supervisor_alive": _pid_alive(supervisor_pid),
        "single_writer": runtime_pid > 0 and runtime_pid == lock_pid and _pid_alive(lock_pid) and (child_pid in {0, runtime_pid}),
        "runtime_uptime": max(0, now - started_at) if started_at > 0 else 0,
        "restart_count": _integer(supervisor.get("restart_count_window"), 0),
        "ledger_writable": ledger_writable,
        "disk": disk,
        "retention_age": _age(now, retention.get("timestamp")),
        "grafana_up": _local_port_up("127.0.0.1", 3000),
    }


def _local_port_up(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.025):
            return True
    except OSError:
        return False


def _safe_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(name: str, value: Any, labels: dict[str, Any] | None = None) -> str:
    numeric = _number(value, 0.0)
    if labels:
        encoded = ",".join(f'{key}="{_safe_label(item)}"' for key, item in labels.items())
        return f"{name}{{{encoded}}} {numeric:.12g}"
    return f"{name} {numeric:.12g}"


def _trade_tape(path: Path, now: int) -> dict[str, Any]:
    rows = 0
    newest_receive_ms = 0
    assets: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                asset = str(row.get("asset_id") or "")
                if asset:
                    assets.add(asset)
                newest_receive_ms = max(newest_receive_ms, _integer(row.get("received_ms"), 0))
    except (OSError, csv.Error):
        pass
    newest_ts = newest_receive_ms / 1000.0 if newest_receive_ms > 0 else 0.0
    return {"rows": rows, "assets": len(assets), "age": _age(now, newest_ts)}


def _maker_latency(path: Path) -> dict[str, Any]:
    stages = ("parse_ns", "book_ns", "feature_ns", "decision_ns", "risk_ns",
              "tx_queue_ns", "execution_ns", "receive_to_intent_ns")
    samples: dict[str, list[int]] = {stage: [] for stage in stages}
    rows = 0
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                for stage in stages:
                    value = _integer(row.get(stage), 0)
                    if value > 0:
                        samples[stage].append(value)
    except (OSError, csv.Error):
        return {"present": False, "rows": 0, "stages": {}}

    def percentile(values: list[int], probability: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        return ordered[int(probability * (len(ordered) - 1))]

    summary: dict[str, dict[str, int]] = {}
    for stage, values in samples.items():
        if not values:
            continue
        summary[stage] = {
            "samples": len(values),
            "p50": percentile(values, 0.50),
            "p90": percentile(values, 0.90),
            "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99),
            "p99_9": percentile(values, 0.999),
            "max": max(values),
        }
    return {"present": rows > 0, "rows": rows, "stages": summary}


def _graph_open_cost(path: Path) -> float | None:
    state = _json(path)
    bundles = state.get("bundles") if isinstance(state.get("bundles"), dict) else None
    if bundles is None:
        return None
    total = 0.0
    for bundle in bundles.values():
        if not isinstance(bundle, dict) or bundle.get("final") is True:
            continue
        legs = bundle.get("legs") if isinstance(bundle.get("legs"), dict) else {}
        for leg in legs.values():
            if not isinstance(leg, dict):
                continue
            for fill in leg.get("fills") if isinstance(leg.get("fills"), list) else []:
                if isinstance(fill, dict):
                    total += max(0.0, _number(fill.get("shares"))) * max(0.0, _number(fill.get("price")))
                    total += max(0.0, _number(fill.get("fee")))
    return total


def collect_snapshot(run_root: Path, repository_root: Path | None = None, *, now: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    run_root = run_root.resolve()
    repository_root = (repository_root or Path(".")).resolve()
    runtime = _json(run_root / "control" / "runtime_status.json")
    portfolio = _json(run_root / "control" / "portfolio_state.json")
    allocations = _json(run_root / "control" / "allocations" / "manifest.json")
    graph = _json(run_root / "graph_rv" / "status.json")
    graph_scan = _json(run_root / "graph_rv" / "scan_status.json")
    hard = _json(run_root / "hard_arb" / "status.json")
    micro = _json(run_root / "micro_taker" / "status.json")
    maker = _json(run_root / "micro_maker" / "status.json")
    external = _json(run_root / "external" / "status.json")
    osint = _json(run_root / "osint" / "status.json")
    market_open = _json(run_root / "market_open" / "status.json")
    economics_path = run_root / "canonical_economics.json"
    canonical = _json(economics_path)
    joint = _json(run_root / "learned_execution" / "joint_policy.json")
    ledger_path = run_root / "ledger" / "execution.jsonl"
    ledger = summarize_ledger(ledger_path)
    maker_lab = summarize_maker_microstructure(
        ledger_path, run_root / "micro_maker" / "reward_selection.json"
    )
    maker_latency = _maker_latency(run_root / "micro_maker" / "latency.csv")
    tape = _trade_tape(run_root / "trade_tape.csv", now)
    directives = _json(repository_root / "config" / "operator_directives.json")
    authorization = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    authority_max_drawdown = _number(authorization.get("max_drawdown"), 0.0)
    authority_valid = directives.get("authority") == "latest_explicit_user_instruction" and authorization.get("paper_only") is True and authorization.get("authenticated_execution") is False and 0.0 < authority_max_drawdown <= 1.0
    sha = _git_head(repository_root)
    runtime_alive = _pid_alive(runtime.get("pid")) and not (run_root / "control" / "KILL").exists()

    budgets = allocations.get("budgets") if isinstance(allocations.get("budgets"), dict) else {}
    sleeves = portfolio.get("sleeves") if isinstance(portfolio.get("sleeves"), dict) else {}
    strategies: dict[str, dict[str, Any]] = {}
    for name, row in sleeves.items():
        if not isinstance(row, dict) or name == "reserve":
            continue
        budget = _number(row.get("budget"))
        equity = _number(row.get("equity"), budget)
        strategies[str(name)] = {"equity": equity, "pnl": equity - budget, "killed": bool(row.get("killed", False))}
    if graph:
        strategies.setdefault("graph_rv", {}).update({"equity": _number(graph.get("equity")), "pnl": _number(graph.get("equity")) - _number(budgets.get("graph_rv")), "killed": bool(graph.get("killed", False)), "signals": _integer(graph_scan.get("bundles"))})
    if hard:
        strategies.setdefault("hard_arb", {}).update({"equity": _number(hard.get("equity_cost_basis"), _number(hard.get("cash"))), "pnl": _number(hard.get("realized_pnl_total")), "killed": bool(hard.get("killed", False)), "signals": _integer(hard.get("candidates"))})
    if micro:
        strategies.setdefault("micro_taker", {}).update({"equity": _number(micro.get("equity")), "pnl": _number(micro.get("equity")) - _number(budgets.get("micro_taker")), "killed": bool(micro.get("killed", False)), "signals": _integer(micro.get("signals")), "best_edge": _number(micro.get("best_edge")), "live_units": _integer(micro.get("open_positions")), "net_pnl": _number(micro.get("realized_pnl_total")), "paper_eligible": False})
    if maker:
        strategies.setdefault("micro_maker", {}).update({"killed": False, "paper_eligible": False})
    if external:
        strategies.setdefault("external", {}).update({"paper_eligible": False})
    if osint:
        strategies.setdefault("osint", {}).update({
            "paper_eligible": False,
            "signals": _integer(osint.get("new_events")),
        })
    if market_open:
        strategies.setdefault("market_open", {}).update({
            "paper_eligible": False,
            "signals": _integer(market_open.get("new_markets")),
        })
    for name, row in ledger.get("strategies", {}).items():
        if not isinstance(row, dict):
            continue
        strategies.setdefault(str(name).lower(), {}).update({
            "ledger_opportunities": _integer(row.get("opportunities")),
            "ledger_orders": _integer(row.get("orders_submitted")),
            "ledger_fills": _integer(row.get("fills")),
            "ledger_complete_fills": _integer(row.get("complete_fills")),
            "ledger_partial_fills": _integer(row.get("partial_fills")),
            "ledger_unwinds": _integer(row.get("unwinds")),
            "ledger_final_pnl": _number(row.get("final_pnl")),
            "ledger_capital_hours": _number(row.get("capital_duration_ms")) / 3_600_000.0,
        })

    starting = _number(portfolio.get("account_starting_capital"), _number(allocations.get("account_starting_capital")))
    equity = _number(portfolio.get("equity"), starting)
    realized = _number(canonical.get("net_pnl"), 0.0)
    gross_components = [_graph_open_cost(run_root / "graph_rv" / "state.json")]
    gross_known = all(value is not None for value in gross_components)
    gross = sum(float(value) for value in gross_components if value is not None) if gross_known else None
    ages = {
        "runtime": _age(now, runtime.get("timestamp")),
        "portfolio": _age(now, portfolio.get("timestamp")),
        "graph": _age(now, graph.get("timestamp")),
        "economics": _file_age(economics_path, now),
        "trade_tape": tape["age"],
    }
    operations = _runtime_operations(run_root, runtime, now)
    return {
        "timestamp": now,
        "sha": sha,
        "run_root": run_root.name,
        "runtime": runtime,
        "runtime_alive": runtime_alive,
        "portfolio": portfolio,
        "allocations": allocations,
        "graph": graph,
        "graph_scan": graph_scan,
        "hard": hard,
        "micro": micro,
        "maker": maker,
        "external": external,
        "osint": osint,
        "market_open": market_open,
        "canonical_economics": canonical,
        "joint_policy": joint,
        "ledger": ledger,
        "maker_lab": maker_lab,
        "maker_latency": maker_latency,
        "trade_tape": tape,
        "authority": {"valid": authority_valid, "max_drawdown": authority_max_drawdown},
        "strategies": strategies,
        "ages": ages,
        "operations": operations,
        "economics": {
            "starting_capital": starting,
            "cash": sum(_number(row.get("equity")) for name, row in sleeves.items() if isinstance(row, dict) and name == "reserve"),
            "equity": equity,
            "pnl": equity - starting,
            "realized_pnl": realized,
            "unrealized_executable_pnl": equity - starting - realized,
            "drawdown": _number(portfolio.get("drawdown")),
            "gross_exposure": gross,
            "capital_utilization": (gross / starting) if gross is not None and starting > 0 else None,
            "live_units": sum(_integer(row.get("open_positions")) for row in (micro,) if isinstance(row, dict)),
            "killed": bool(portfolio.get("killed", False)),
        },
    }


def health_reasons(snapshot: dict[str, Any], *, max_runtime_age: int = 180, max_supervisor_age: int = 30) -> list[str]:
    reasons: list[str] = []
    runtime = snapshot["runtime"]
    portfolio = snapshot["portfolio"]
    graph = snapshot["graph"]
    canonical = snapshot["canonical_economics"]
    ledger = snapshot["ledger"]
    ages = snapshot["ages"]
    authority = snapshot["authority"]
    if authority.get("valid") is not True: reasons.append("operator_authority_missing_or_invalid")
    if runtime.get("version") != 7: reasons.append("runtime_version_not_v7")
    if runtime.get("paper_only") is not True: reasons.append("runtime_not_paper_only")
    if runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False: reasons.append("authenticated_execution_not_disabled")
    if runtime.get("model_sha") != snapshot.get("sha"): reasons.append("runtime_sha_mismatch")
    if snapshot.get("runtime_alive") is not True: reasons.append("execution_not_alive")
    if portfolio.get("paper_only") is not True or portfolio.get("authenticated_execution") is not False: reasons.append("portfolio_guard_contract_invalid")
    if graph.get("paper_only") is not True or graph.get("authenticated_execution") is not False: reasons.append("graph_runtime_missing_or_unsafe")
    if canonical.get("paper_only") is not True or canonical.get("authenticated_execution") is not False: reasons.append("canonical_economics_missing_or_unsafe")
    if canonical.get("expected_model_sha") != snapshot.get("sha"): reasons.append("canonical_economics_sha_mismatch")
    if not ledger.get("present"): reasons.append("canonical_ledger_missing")
    elif not ledger.get("valid"): reasons.append("canonical_ledger_invalid_or_mixed_sha")
    if _integer(snapshot["trade_tape"].get("rows")) <= 0: reasons.append("trade_tape_empty")
    for key in ("runtime", "graph", "economics", "trade_tape"):
        if not math.isfinite(float(ages[key])) or float(ages[key]) > max_runtime_age: reasons.append(f"{key}_stale")
    if not math.isfinite(float(ages["portfolio"])) or float(ages["portfolio"]) > max_supervisor_age: reasons.append("portfolio_guard_stale")
    if snapshot["economics"]["killed"]: reasons.append("runtime_killed")
    max_drawdown = _number(authority.get("max_drawdown"), 0.0)
    if max_drawdown > 0.0 and snapshot["economics"]["drawdown"] >= max_drawdown - 1e-12: reasons.append("drawdown_limit_breached")
    return sorted(set(reasons))


def _append_maker_lab_metrics(lines: list[str], lab: dict[str, Any]) -> None:
    quality = lab.get("quality") if isinstance(lab.get("quality"), dict) else {}
    lines.extend([
        _metric("polymarket_maker_lab_present", 1 if lab.get("present") else 0),
        _metric("polymarket_maker_lab_orders", lab.get("orders")),
        _metric("polymarket_maker_lab_filled_orders", lab.get("filled_orders")),
        _metric("polymarket_maker_lab_fills", lab.get("fills")),
        _metric("polymarket_maker_lab_realized_pnl_usd", lab.get("realized_pnl")),
        _metric("polymarket_maker_lab_attributed_realized_pnl_usd", lab.get("attributed_realized_pnl")),
        _metric("polymarket_maker_lab_linked_fills", quality.get("linked_fills")),
        _metric("polymarket_maker_lab_unlinked_fills", quality.get("unlinked_fills")),
        _metric("polymarket_maker_lab_linked_markouts", quality.get("linked_markouts")),
        _metric("polymarket_maker_lab_unlinked_markouts", quality.get("unlinked_markouts")),
        _metric("polymarket_maker_lab_ofi_exact_orders", quality.get("ofi_exact_orders")),
        _metric("polymarket_maker_lab_ofi_proxy_orders", quality.get("ofi_proxy_orders")),
        _metric("polymarket_maker_lab_reward_known_orders", quality.get("reward_known_orders")),
        _metric("polymarket_maker_lab_unattributed_sell_fills", quality.get("unattributed_sell_fills")),
        _metric("polymarket_maker_lab_unattributed_merge_pnl_usd", quality.get("unattributed_merge_pnl")),
        _metric("polymarket_maker_lab_measurement_info", 1, {
            "ofi": quality.get("ofi_source", "unknown"),
            "reward": quality.get("reward_source", "unknown"),
            "pnl_attribution": quality.get("merge_pnl_attribution", "unknown"),
        }),
    ])
    for horizon, count in sorted((lab.get("markouts") or {}).items()):
        lines.append(_metric("polymarket_maker_lab_markout_observations_total", count, {"horizon": horizon}))

    for row in lab.get("segments") if isinstance(lab.get("segments"), list) else []:
        if not isinstance(row, dict):
            continue
        labels = {
            "action": row.get("action", "UNKNOWN"),
            "variant": row.get("variant", "UNKNOWN"),
            "dimension": row.get("dimension", "unknown"),
            "bucket": row.get("bucket", "UNKNOWN"),
        }
        lines.append(_metric("polymarket_maker_lab_segment_orders", row.get("orders"), labels))
        lines.append(_metric("polymarket_maker_lab_segment_filled_orders", row.get("filled_orders"), labels))
        lines.append(_metric("polymarket_maker_lab_segment_fills", row.get("fills"), labels))
        lines.append(_metric("polymarket_maker_lab_segment_filled_shares", row.get("filled_shares"), labels))
        lines.append(_metric("polymarket_maker_lab_segment_realized_pnl_usd", row.get("realized_pnl"), labels))
        markout_pnl = row.get("markout_pnl") if isinstance(row.get("markout_pnl"), dict) else {}
        markout_shares = row.get("markout_shares") if isinstance(row.get("markout_shares"), dict) else {}
        markout_count = row.get("markout_count") if isinstance(row.get("markout_count"), dict) else {}
        for horizon in sorted(markout_count):
            hlabels = dict(labels)
            hlabels["horizon"] = horizon
            lines.append(_metric("polymarket_maker_lab_segment_markout_pnl_usd", markout_pnl.get(horizon), hlabels))
            lines.append(_metric("polymarket_maker_lab_segment_markout_shares", markout_shares.get(horizon), hlabels))
            lines.append(_metric("polymarket_maker_lab_segment_markout_observations", markout_count.get(horizon), hlabels))

    for row in lab.get("conditionals") if isinstance(lab.get("conditionals"), list) else []:
        if not isinstance(row, dict):
            continue
        labels = {
            "action": row.get("action", "UNKNOWN"),
            "toxicity": row.get("toxicity", "UNKNOWN"),
            "queue": row.get("queue", "UNKNOWN"),
        }
        lines.append(_metric("polymarket_maker_lab_conditional_orders", row.get("orders"), labels))
        lines.append(_metric("polymarket_maker_lab_conditional_filled_orders", row.get("filled_orders"), labels))
        lines.append(_metric("polymarket_maker_lab_conditional_realized_pnl_usd", row.get("realized_pnl"), labels))
        markout_pnl = row.get("markout_pnl") if isinstance(row.get("markout_pnl"), dict) else {}
        markout_shares = row.get("markout_shares") if isinstance(row.get("markout_shares"), dict) else {}
        markout_count = row.get("markout_count") if isinstance(row.get("markout_count"), dict) else {}
        for horizon in sorted(markout_count):
            hlabels = dict(labels)
            hlabels["horizon"] = horizon
            lines.append(_metric("polymarket_maker_lab_conditional_markout_pnl_usd", markout_pnl.get(horizon), hlabels))
            lines.append(_metric("polymarket_maker_lab_conditional_markout_shares", markout_shares.get(horizon), hlabels))
            lines.append(_metric("polymarket_maker_lab_conditional_markout_observations", markout_count.get(horizon), hlabels))

    for row in lab.get("markets") if isinstance(lab.get("markets"), list) else []:
        if not isinstance(row, dict):
            continue
        labels = {"market": row.get("market", "UNKNOWN"), "action": row.get("action", "UNKNOWN")}
        lines.append(_metric("polymarket_maker_lab_market_orders", row.get("orders"), labels))
        lines.append(_metric("polymarket_maker_lab_market_filled_orders", row.get("filled_orders"), labels))
        lines.append(_metric("polymarket_maker_lab_market_realized_pnl_usd", row.get("realized_pnl"), labels))
        markout_pnl = row.get("markout_pnl") if isinstance(row.get("markout_pnl"), dict) else {}
        markout_shares = row.get("markout_shares") if isinstance(row.get("markout_shares"), dict) else {}
        for horizon in sorted(markout_pnl):
            hlabels = dict(labels)
            hlabels["horizon"] = horizon
            lines.append(_metric("polymarket_maker_lab_market_markout_pnl_usd", markout_pnl.get(horizon), hlabels))
            lines.append(_metric("polymarket_maker_lab_market_markout_shares", markout_shares.get(horizon), hlabels))


def render_prometheus(snapshot: dict[str, Any]) -> str:
    runtime = snapshot["runtime"]
    ledger = snapshot["ledger"]
    ledger_total = ledger.get("total") if isinstance(ledger.get("total"), dict) else {}
    economics = snapshot["economics"]
    authority = snapshot["authority"]
    canonical = snapshot["canonical_economics"]
    maker_lab = snapshot.get("maker_lab") if isinstance(snapshot.get("maker_lab"), dict) else {}
    maker_latency = snapshot.get("maker_latency") if isinstance(snapshot.get("maker_latency"), dict) else {}
    operations = snapshot.get("operations") if isinstance(snapshot.get("operations"), dict) else {}
    disk = operations.get("disk") if isinstance(operations.get("disk"), dict) else {}
    supervisor = operations.get("supervisor") if isinstance(operations.get("supervisor"), dict) else {}
    osint = snapshot.get("osint") if isinstance(snapshot.get("osint"), dict) else {}
    market_open = snapshot.get("market_open") if isinstance(snapshot.get("market_open"), dict) else {}
    labels = {"adapter":"v7_native","run_root":snapshot["run_root"],"version":"v7"}
    lines = [
        "# TYPE polymarket_v7_runtime_info gauge",
        _metric("polymarket_v7_runtime_info", 1 if runtime.get("version") == 7 else 0),
        _metric("polymarket_runtime_info", 1, labels),
        _metric("polymarket_v7_deployed_sha_info", 1, {"sha": snapshot["sha"]}),
        _metric("polymarket_v7_runtime_identity_info", 1, {
            "source_sha": runtime.get("model_sha", "UNKNOWN"),
            "config_hash": runtime.get("config_hash", "UNKNOWN"),
            "policy_hash": runtime.get("policy_hash", "UNKNOWN"),
            "model_hash": runtime.get("model_hash", "UNKNOWN"),
            "run_id": runtime.get("run_id", "UNKNOWN"),
            "ledger_id": runtime.get("ledger_id", "UNKNOWN"),
            "server_id": runtime.get("server_id", "UNKNOWN"),
        }),
        _metric("polymarket_v7_operator_authority_valid", 1 if authority.get("valid") else 0),
        _metric("polymarket_v7_authority_max_drawdown_ratio", authority.get("max_drawdown")),
        _metric("polymarket_v7_paper_only_contract_ok", 1 if runtime.get("paper_only") is True and snapshot["portfolio"].get("paper_only") is True else 0),
        _metric("polymarket_v7_authenticated_execution_disabled", 1 if runtime.get("authenticated_execution") is False and runtime.get("real_order_submission") is False and snapshot["portfolio"].get("authenticated_execution") is False else 0),
        _metric("polymarket_v7_execution_alive", 1 if snapshot["runtime_alive"] else 0),
        _metric("polymarket_v7_component_ready", 1 if snapshot["runtime_alive"] else 0, {"component": "core_runtime"}),
        _metric("polymarket_v7_supervisor_alive", 1 if operations.get("supervisor_alive") else 0),
        _metric("polymarket_v7_monitoring_component_up", 1, {"component": "exporter"}),
        _metric("polymarket_v7_monitoring_component_up", 1 if operations.get("grafana_up") else 0, {"component": "grafana"}),
        _metric("polymarket_v7_supervisor_info", 1, {"state": supervisor.get("state", "UNKNOWN")}),
        _metric("polymarket_v7_runtime_uptime_seconds", operations.get("runtime_uptime")),
        _metric("polymarket_v7_restart_count_window", operations.get("restart_count")),
        _metric("polymarket_v7_single_writer_ok", 1 if operations.get("single_writer") else 0),
        _metric("polymarket_v7_exact_sha_ok", 1 if runtime.get("model_sha") == snapshot["sha"] else 0),
        _metric("polymarket_v7_ledger_writable", 1 if operations.get("ledger_writable") else 0),
        _metric("polymarket_v7_disk_free_bytes", disk.get("free_bytes")),
        _metric("polymarket_v7_disk_free_ratio", disk.get("free_ratio")),
        _metric("polymarket_v7_retention_status_age_seconds", 0 if not math.isfinite(float(operations.get("retention_age", math.inf))) else operations.get("retention_age")),
        _metric("polymarket_runtime_equity_usd", economics["equity"]),
        _metric("polymarket_runtime_pnl_usd", economics["pnl"]),
        _metric("polymarket_runtime_realized_pnl_usd", economics["realized_pnl"]),
        _metric("polymarket_runtime_unrealized_executable_pnl_usd", economics["unrealized_executable_pnl"]),
        _metric("polymarket_runtime_drawdown_ratio", economics["drawdown"]),
        _metric("polymarket_runtime_live_units", economics["live_units"]),
        _metric("polymarket_runtime_killed", 1 if economics["killed"] else 0),
        _metric("polymarket_v7_trade_tape_rows", snapshot["trade_tape"]["rows"]),
        _metric("polymarket_v7_trade_tape_assets", snapshot["trade_tape"]["assets"]),
        _metric("polymarket_v7_canonical_economics_promotion_ready", 1 if canonical.get("promotion_ready") else 0),
        _metric("polymarket_v7_canonical_submitted_units", canonical.get("submitted_units")),
        _metric("polymarket_v7_canonical_complete_units", canonical.get("complete_units")),
        _metric("polymarket_v7_ledger_present", 1 if ledger.get("present") else 0),
        _metric("polymarket_v7_ledger_valid", 1 if ledger.get("valid") else 0),
        _metric("polymarket_v7_ledger_rows", _integer(ledger.get("rows"))),
        _metric("polymarket_v7_ledger_invalid_rows", _integer(ledger.get("invalid_rows"))),
        _metric("polymarket_v7_ledger_model_sha_count", len(ledger.get("model_shas") or [])),
        _metric("polymarket_v7_latency_samples_present", 1 if maker_latency.get("present") else 0),
        _metric("polymarket_v7_latency_rows", maker_latency.get("rows")),
        _metric("polymarket_v7_osint_enabled_sources", osint.get("enabled_sources")),
        _metric("polymarket_v7_osint_healthy_sources", osint.get("healthy_sources")),
        _metric("polymarket_v7_osint_new_events", osint.get("new_events")),
        _metric("polymarket_v7_market_open_tracked_markets", market_open.get("tracked_markets")),
        _metric("polymarket_v7_market_open_new_markets", market_open.get("new_markets")),
        _metric("polymarket_v7_market_open_emitted_milestones", market_open.get("emitted_milestones")),
        _metric("polymarket_v7_market_open_semantic_verified", market_open.get("semantic_verified_markets")),
        _metric("polymarket_execution_opportunities", _integer(ledger_total.get("opportunities"))),
        _metric("polymarket_execution_candidates", _integer(ledger_total.get("candidates"))),
        _metric("polymarket_execution_makes", _integer(ledger_total.get("makes"))),
        _metric("polymarket_execution_takes", _integer(ledger_total.get("takes"))),
        _metric("polymarket_execution_arbs", _integer(ledger_total.get("arbs"))),
        _metric("polymarket_execution_cancels", _integer(ledger_total.get("cancels"))),
        _metric("polymarket_execution_withdraws", _integer(ledger_total.get("withdraws"))),
        _metric("polymarket_execution_orders_submitted", _integer(ledger_total.get("orders_submitted"))),
        _metric("polymarket_execution_effective_orders", _integer(ledger_total.get("effective_orders"))),
        _metric("polymarket_execution_fills", _integer(ledger_total.get("fills"))),
        _metric("polymarket_execution_complete_fills", _integer(ledger_total.get("complete_fills"))),
        _metric("polymarket_execution_partial_fills", _integer(ledger_total.get("partial_fills"))),
        _metric("polymarket_execution_unwinds", _integer(ledger_total.get("unwinds"))),
        _metric("polymarket_execution_final_pnl_usd", _number(ledger_total.get("final_pnl"))),
        _metric("polymarket_execution_capital_hours", _number(ledger_total.get("capital_duration_ms")) / 3_600_000.0),
    ]
    if economics["gross_exposure"] is not None:
        lines.append(_metric("polymarket_runtime_gross_exposure_usd", economics["gross_exposure"]))
    if economics["capital_utilization"] is not None:
        lines.append(_metric("polymarket_runtime_capital_utilization_ratio", economics["capital_utilization"]))
    for name, age in snapshot["ages"].items():
        lines.append(_metric("polymarket_v7_state_age_seconds", 0 if not math.isfinite(float(age)) else age, {"surface":name}))
        lines.append(_metric("polymarket_v7_state_present", 1 if math.isfinite(float(age)) else 0, {"surface":name}))
    for horizon, total in (ledger_total.get("markout_sum") or {}).items():
        count = _integer((ledger_total.get("markout_count") or {}).get(horizon))
        if count:
            lines.append(_metric("polymarket_execution_mean_markout", _number(total)/count, {"horizon":horizon}))
            lines.append(_metric("polymarket_execution_markout_observations", count, {"horizon":horizon}))
    for strategy, row in sorted(snapshot["strategies"].items()):
        labels_s={"strategy":strategy}
        for key, metric_name in (("equity","polymarket_strategy_equity_usd"),("pnl","polymarket_strategy_pnl_usd"),("signals","polymarket_strategy_opportunities"),("best_edge","polymarket_strategy_best_edge"),("live_units","polymarket_strategy_live_units"),("net_pnl","polymarket_strategy_evidence_net_pnl_usd"),("ledger_opportunities","polymarket_strategy_ledger_opportunities"),("ledger_orders","polymarket_strategy_ledger_orders_submitted"),("ledger_fills","polymarket_strategy_ledger_fills"),("ledger_complete_fills","polymarket_strategy_complete_fills"),("ledger_partial_fills","polymarket_strategy_partial_fills"),("ledger_unwinds","polymarket_strategy_unwinds"),("ledger_final_pnl","polymarket_strategy_final_pnl_usd"),("ledger_capital_hours","polymarket_strategy_capital_hours")):
            if key in row and row[key] is not None: lines.append(_metric(metric_name,row[key],labels_s))
        if "killed" in row: lines.append(_metric("polymarket_strategy_killed",1 if row["killed"] else 0,labels_s))
        if "paper_eligible" in row: lines.append(_metric("polymarket_strategy_paper_eligible",1 if row["paper_eligible"] else 0,labels_s))
    _append_maker_lab_metrics(lines, maker_lab)
    for source in osint.get("sources") if isinstance(osint.get("sources"), list) else []:
        if not isinstance(source, dict):
            continue
        source_labels = {"source": source.get("source_id", "UNKNOWN")}
        lines.append(_metric("polymarket_v7_osint_source_healthy", 1 if source.get("healthy") else 0,
                             source_labels))
        lines.append(_metric("polymarket_v7_osint_source_new_events", source.get("new_events"),
                             source_labels))
    for stage, row in sorted((maker_latency.get("stages") or {}).items()):
        if not isinstance(row, dict):
            continue
        lines.append(_metric("polymarket_v7_latency_stage_samples", row.get("samples"), {"stage": stage}))
        for percentile in ("p50", "p90", "p95", "p99", "p99_9", "max"):
            lines.append(_metric(
                "polymarket_v7_latency_stage_nanoseconds",
                row.get(percentile),
                {"stage": stage, "percentile": percentile},
            ))
    return "\n".join(lines)+"\n"


class ExporterHandler(BaseHTTPRequestHandler):
    run_root=Path("runs/paper_v7_live"); repository_root=Path("."); max_runtime_age=180; max_supervisor_age=30
    def log_message(self,_format:str,*_args:object)->None: return
    def do_GET(self)->None:  # noqa: N802
        snapshot=collect_snapshot(self.run_root,self.repository_root)
        if self.path=="/metrics": payload=render_prometheus(snapshot).encode(); self.send_response(200); self.send_header("Content-Type","text/plain; version=0.0.4; charset=utf-8")
        elif self.path=="/healthz":
            reasons=health_reasons(snapshot,max_runtime_age=self.max_runtime_age,max_supervisor_age=self.max_supervisor_age); payload=(json.dumps({"ok":not reasons,"reasons":reasons},sort_keys=True)+"\n").encode(); self.send_response(200 if not reasons else 503); self.send_header("Content-Type","application/json; charset=utf-8")
        else: payload=b"not found\n"; self.send_response(404); self.send_header("Content-Type","text/plain; charset=utf-8")
        self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--run-root",type=Path,default=Path("runs/paper_v7_live")); parser.add_argument("--repository-root",type=Path,default=Path(".")); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=9108); parser.add_argument("--max-runtime-age",type=int,default=180); parser.add_argument("--max-supervisor-age",type=int,default=30); args=parser.parse_args()
    ExporterHandler.run_root=args.run_root; ExporterHandler.repository_root=args.repository_root; ExporterHandler.max_runtime_age=max(1,args.max_runtime_age); ExporterHandler.max_supervisor_age=max(1,args.max_supervisor_age)
    server=ThreadingHTTPServer((args.host,args.port),ExporterHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0

if __name__=="__main__": raise SystemExit(main())
