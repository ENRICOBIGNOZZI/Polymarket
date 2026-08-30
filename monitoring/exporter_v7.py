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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from v7_ledger_metrics import summarize_ledger
from v7_external_fair import summarize_external_fair
from v7_maker_fillability_exact import summarize_best_available_fillability
from v7_maker_microstructure import summarize_maker_microstructure

_FILLABILITY_CACHE_KEY: tuple[str, str, int] | None = None
_FILLABILITY_CACHE_VALUE: dict[str, Any] | None = None
_FILLABILITY_REFRESH_SECONDS = 30
_MAKER_SELECTOR_OPERATIONAL_STATES = {
    "OPERATIONAL_REWARDED",
    "OPERATIONAL_FALLBACK",
    "OPERATIONAL_RECENT_FLOW",
}


def _fillability_report(run_root: Path, repository_root: Path, runtime_sha: str, now: int) -> dict[str, Any]:
    global _FILLABILITY_CACHE_KEY, _FILLABILITY_CACHE_VALUE
    key = (str(run_root.resolve()), runtime_sha, now // _FILLABILITY_REFRESH_SECONDS)
    if key == _FILLABILITY_CACHE_KEY and _FILLABILITY_CACHE_VALUE is not None:
        return _FILLABILITY_CACHE_VALUE
    report = summarize_best_available_fillability(
        run_root / "ledger" / "execution.jsonl",
        run_root / "trade_tape.csv",
        repository_root / "config" / "v7_professional_market_maker.json",
        model_sha=runtime_sha or None,
        now_ms=now * 1000,
    )
    _FILLABILITY_CACHE_KEY = key
    _FILLABILITY_CACHE_VALUE = report
    return report


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
    research_sleeves = _json(run_root / "control" / "research_sleeves_manifest.json")
    research_shadow_statuses = {
        family: _json(run_root / "shadow" / family / "status.json")
        for family in ("sports_latency", "cross_platform", "wallet_intelligence")
    }
    portfolio = _json(run_root / "control" / "portfolio_state.json")
    allocations = _json(run_root / "control" / "allocations" / "manifest.json")
    graph = _json(run_root / "graph_rv" / "status.json")
    graph_scan = _json(run_root / "graph_rv" / "scan_status.json")
    hard = _json(run_root / "hard_arb" / "status.json")
    micro = _json(run_root / "micro_taker" / "status.json")
    maker = _json(run_root / "micro_maker" / "status.json")
    maker_diagnostics = _json(run_root / "micro_maker" / "runtime_diagnostics.json")
    maker_selector = _json(run_root / "micro_maker" / "selector_status.json")
    external = _json(run_root / "external" / "status.json")
    osint = _json(run_root / "osint" / "status.json")
    osint_mapping = _json(run_root / "osint" / "mapping_status.json")
    market_open = _json(run_root / "market_open" / "status.json")
    universe = _json(run_root / "universe" / "status.json")
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
    strategy_registry = _json(repository_root / "config" / "v7_strategy_registry.json")
    live_model_scope = _json(repository_root / "config" / "v7_live_model_scope.json")
    authorization = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    authority_max_drawdown = _number(authorization.get("max_drawdown"), 0.0)
    authority_valid = directives.get("authority") == "latest_explicit_user_instruction" and authorization.get("paper_only") is True and authorization.get("authenticated_execution") is False and 0.0 < authority_max_drawdown <= 1.0
    sha = _git_head(repository_root)
    runtime_alive = _pid_alive(runtime.get("pid")) and not (run_root / "control" / "KILL").exists()
    runtime_sha = str(runtime.get("model_sha") or "")
    maker_fillability = _fillability_report(run_root, repository_root, runtime_sha, now)
    external_fair = summarize_external_fair(
        run_root, repository_root, runtime_sha=runtime_sha, now_s=now,
    )

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
        "research_sleeves": research_sleeves,
        "research_shadow_statuses": research_shadow_statuses,
        "strategy_registry": strategy_registry,
        "live_model_scope": live_model_scope,
        "runtime_alive": runtime_alive,
        "portfolio": portfolio,
        "allocations": allocations,
        "graph": graph,
        "graph_scan": graph_scan,
        "hard": hard,
        "micro": micro,
        "maker": maker,
        "maker_diagnostics": maker_diagnostics,
        "maker_selector": maker_selector,
        "external": external,
        "osint": osint,
        "osint_mapping": osint_mapping,
        "market_open": market_open,
        "universe": universe,
        "canonical_economics": canonical,
        "joint_policy": joint,
        "ledger": ledger,
        "maker_lab": maker_lab,
        "maker_fillability": maker_fillability,
        "external_fair": external_fair,
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
    research = snapshot.get("research_sleeves") if isinstance(snapshot.get("research_sleeves"), dict) else {}
    research_statuses = snapshot.get("research_shadow_statuses") if isinstance(snapshot.get("research_shadow_statuses"), dict) else {}
    registry = snapshot.get("strategy_registry") if isinstance(snapshot.get("strategy_registry"), dict) else {}
    live_scope = snapshot.get("live_model_scope") if isinstance(snapshot.get("live_model_scope"), dict) else {}
    portfolio = snapshot["portfolio"]
    graph = snapshot["graph"]
    canonical = snapshot["canonical_economics"]
    ledger = snapshot["ledger"]
    ages = snapshot["ages"]
    authority = snapshot["authority"]
    maker = snapshot.get("maker") if isinstance(snapshot.get("maker"), dict) else {}
    maker_diagnostics = snapshot.get("maker_diagnostics") if isinstance(snapshot.get("maker_diagnostics"), dict) else {}
    selector = snapshot.get("maker_selector") if isinstance(snapshot.get("maker_selector"), dict) else {}
    external_fair = snapshot.get("external_fair") if isinstance(snapshot.get("external_fair"), dict) else {}
    if authority.get("valid") is not True: reasons.append("operator_authority_missing_or_invalid")
    if runtime.get("version") != 7: reasons.append("runtime_version_not_v7")
    if runtime.get("paper_only") is not True: reasons.append("runtime_not_paper_only")
    if runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False: reasons.append("authenticated_execution_not_disabled")
    if runtime.get("model_sha") != snapshot.get("sha"): reasons.append("runtime_sha_mismatch")
    try: maker_age_ms = int(snapshot.get("timestamp") or 0) * 1000 - int(maker.get("timestamp_ms") or 0)
    except (TypeError, ValueError, OverflowError): maker_age_ms = (max_runtime_age + 1) * 1000
    if (
        maker.get("schema") != "polymarket_v7_professional_maker_status_v1"
        or maker.get("model_sha") != snapshot.get("sha")
        or maker.get("paper_only") is not True
        or maker.get("authenticated_execution") is not False
        or maker.get("killed") is True
        or maker.get("source") in {None, "", "not_started"}
        or maker_age_ms < -5_000
        or maker_age_ms > max_runtime_age * 1000
    ): reasons.append("professional_maker_missing_stale_or_unsafe")
    try: selector_age_ms = int(snapshot.get("timestamp") or 0) * 1000 - int(selector.get("timestamp_ms") or 0)
    except (TypeError, ValueError, OverflowError): selector_age_ms = (max_runtime_age + 1) * 1000
    if (
        selector.get("schema") != "polymarket_v7_maker_selector_status_v1"
        or selector.get("model_sha") != snapshot.get("sha")
        or selector.get("paper_only") is not True
        or selector.get("authenticated_execution") is not False
        or selector.get("real_order_submission") is not False
        or selector.get("ready") is not True
        or selector.get("state") not in _MAKER_SELECTOR_OPERATIONAL_STATES
        or selector_age_ms < -5_000
        or selector_age_ms > max_runtime_age * 1000
    ): reasons.append("maker_selector_missing_stale_or_unsafe")
    expected_research = {
        "sports_latency", "cross_platform", "wallet_intelligence",
    }
    registered = registry.get("strategies") if isinstance(registry.get("strategies"), list) else []
    registered_names = {
        str(row.get("family") or "") for row in registered if isinstance(row, dict) and row.get("enabled") is True
    }
    if len(registered_names) != 15: reasons.append("strategy_registry_not_15_enabled")
    target_live = set(live_scope.get("target_live_families") or [])
    excluded_live = set(live_scope.get("excluded_live_families") or [])
    if (live_scope.get("schema") != "polymarket_v7_live_model_scope_v1"
            or live_scope.get("target_live_count") != 12
            or live_scope.get("paper_only") is not True
            or live_scope.get("authenticated_execution") is not False
            or live_scope.get("real_order_submission") is not False): reasons.append("live_model_scope_missing_or_invalid")
    if target_live | excluded_live != registered_names or target_live & excluded_live: reasons.append("live_model_scope_not_registry_partition")
    if excluded_live != {"ranking", "pca", "local_factor"}: reasons.append("live_model_scope_exclusions_invalid")
    if set(live_scope.get("research_shadow_supervised_families") or []) != expected_research: reasons.append("live_model_scope_shadow_set_invalid")
    governance = live_scope.get("governance") if isinstance(live_scope.get("governance"), dict) else {}
    if (governance.get("single_execution_owner") is not True
            or governance.get("research_has_capital") is not False
            or governance.get("research_has_oms_authority") is not False
            or governance.get("research_has_ledger_writer_authority") is not False
            or governance.get("automatic_promotion") is not False): reasons.append("live_model_scope_governance_invalid")
    research_rows = research.get("families") if isinstance(research.get("families"), dict) else {}
    if research.get("schema") != "polymarket_v7_research_sleeves_manifest_v1": reasons.append("research_sleeves_manifest_missing_or_invalid")
    if research.get("version") != 7 or research.get("model_sha") != snapshot.get("sha"): reasons.append("research_sleeves_identity_drift")
    if research.get("paper_only") is not True or research.get("authenticated_execution") is not False or research.get("real_order_submission") is not False: reasons.append("research_sleeves_execution_authority_unsafe")
    if set(research_rows) != expected_research: reasons.append("research_sleeves_not_3_exact")
    if not _pid_alive(research.get("supervisor_pid")): reasons.append("research_sleeves_supervisor_dead")
    try: research_age = int(snapshot.get("timestamp") or 0) - int(research.get("timestamp") or 0)
    except (TypeError, ValueError, OverflowError): research_age = max_runtime_age + 1
    if research_age < -5 or research_age > max_runtime_age: reasons.append("research_sleeves_manifest_stale")
    for family in expected_research:
        row = research_rows.get(family) if isinstance(research_rows.get(family), dict) else {}
        status = research_statuses.get(family) if isinstance(research_statuses.get(family), dict) else {}
        if row.get("authority") != "RESEARCH" or row.get("process_state") != "RUNNING": reasons.append(f"research_sleeve_not_running:{family}")
        if row.get("paper_only") is not True or row.get("authenticated_execution") is not False or row.get("real_order_submission") is not False: reasons.append(f"research_sleeve_unsafe:{family}")
        if any(row.get(key) is not False for key in ("execution_authority", "capital_authority", "oms_authority", "ledger_write_authority", "promotion_authority")): reasons.append(f"research_sleeve_authority_violation:{family}")
        evidence_state = row.get("evidence_state")
        if family == "wallet_intelligence":
            if evidence_state != "BLOCKED_CONFIG" or row.get("last_attempt_ts") != 0 or row.get("last_success_ts") != 0:
                reasons.append(f"research_sleeve_unsubstantiated_state:{family}")
        else:
            if evidence_state not in {"BLOCKED_EXTERNAL", "ACTIVE"}:
                reasons.append(f"research_sleeve_component_evidence_missing:{family}")
            if row.get("implementation_complete") is not True or int(row.get("last_attempt_ts") or 0) <= 0:
                reasons.append(f"research_sleeve_component_not_attempted:{family}")
            if evidence_state == "ACTIVE" and (
                row.get("feed_operational") is not True
                or int(row.get("verified_mappings") or 0) <= 0
                or row.get("forward_collection_active") is not True
            ):
                reasons.append(f"research_sleeve_false_active:{family}")
            if evidence_state == "BLOCKED_EXTERNAL" and not str(row.get("blocker") or ""):
                reasons.append(f"research_sleeve_blocker_missing:{family}")
        status_path = Path(str(row.get("status_path") or ""))
        output_path = Path(str(row.get("output_path") or ""))
        if ".." in status_path.parts or tuple(status_path.parts[-3:]) != ("shadow", family, "status.json"): reasons.append(f"research_sleeve_status_path_invalid:{family}")
        if ".." in output_path.parts or tuple(output_path.parts[-2:]) != ("shadow", family): reasons.append(f"research_sleeve_output_path_invalid:{family}")
        if status.get("schema") != "polymarket_v7_research_shadow_status_v1" or status.get("model_sha") != snapshot.get("sha") or status.get("family") != family: reasons.append(f"research_sleeve_status_missing_or_invalid:{family}")
        if status.get("evidence_state") != evidence_state:
            reasons.append(f"research_sleeve_status_manifest_mismatch:{family}")
        if family == "wallet_intelligence":
            if status.get("evidence_state") != "BLOCKED_CONFIG" or status.get("last_attempt_ts") != 0 or status.get("last_success_ts") != 0:
                reasons.append(f"research_sleeve_status_unsubstantiated:{family}")
        elif (
            status.get("implementation_complete") is not True
            or int(status.get("last_attempt_ts") or 0) <= 0
            or status.get("feed_status") != row.get("feed_status")
            or status.get("mapping_status") != row.get("mapping_status")
            or int(status.get("verified_mappings") or 0) != int(row.get("verified_mappings") or 0)
        ):
            reasons.append(f"research_sleeve_status_unsubstantiated:{family}")
        if status.get("paper_only") is not True or status.get("authenticated_execution") is not False or status.get("real_order_submission") is not False: reasons.append(f"research_sleeve_status_unsafe:{family}")
        try: status_age = int(snapshot.get("timestamp") or 0) - int(status.get("timestamp") or 0)
        except (TypeError, ValueError, OverflowError): status_age = max_runtime_age + 1
        if status_age < -5 or status_age > max_runtime_age: reasons.append(f"research_sleeve_status_stale:{family}")
    osint = snapshot.get("osint") if isinstance(snapshot.get("osint"), dict) else {}
    osint_mapping = snapshot.get("osint_mapping") if isinstance(snapshot.get("osint_mapping"), dict) else {}
    market_open = snapshot.get("market_open") if isinstance(snapshot.get("market_open"), dict) else {}
    universe = snapshot.get("universe") if isinstance(snapshot.get("universe"), dict) else {}
    if (universe.get("schema") != "polymarket_v7_adaptive_universe_status_v1"
            or universe.get("model_sha") != snapshot.get("sha")
            or universe.get("state") != "OPERATIONAL"
            or universe.get("discovery_exhaustive") is not True
            or universe.get("pagination_loop_guard_hit") is not False
            or universe.get("paper_only") is not True
            or universe.get("authenticated_execution") is not False
            or universe.get("real_order_submission") is not False
            or _integer(universe.get("eligible_markets")) <= 0):
        reasons.append("adaptive_universe_missing_incomplete_or_unsafe")
    try: universe_age_ms = int(snapshot.get("timestamp") or 0) * 1000 - int(universe.get("timestamp_ms") or 0)
    except (TypeError, ValueError, OverflowError): universe_age_ms = (max_runtime_age + 1) * 1000
    if universe_age_ms < -5_000 or universe_age_ms > max_runtime_age * 1000:
        reasons.append("adaptive_universe_stale")
    if osint.get("schema") != "polymarket_v7_osint_collector_status_v1" or osint.get("paper_only") is not True or osint.get("authenticated_execution") is not False or osint.get("real_order_submission") is not False: reasons.append("osint_live_collector_missing_or_unsafe")
    if (
        osint_mapping.get("schema") != "polymarket_v7_osint_mapping_status_v1"
        or osint_mapping.get("model_sha") != snapshot.get("sha")
        or osint_mapping.get("implementation_complete") is not True
        or osint_mapping.get("mapping_pipeline") is not True
        or osint_mapping.get("paper_only") is not True
        or osint_mapping.get("authenticated_execution") is not False
        or osint_mapping.get("real_order_submission") is not False
        or osint_mapping.get("title_similarity_verification_forbidden") is not True
    ):
        reasons.append("osint_mapping_pipeline_missing_or_unsafe")
    if market_open.get("schema") != "polymarket_v7_market_open_collector_status_v1" or market_open.get("paper_only") is not True or market_open.get("authenticated_execution") is not False or market_open.get("real_order_submission") is not False: reasons.append("market_open_live_collector_missing_or_unsafe")
    for name, status in (("osint", osint), ("market_open", market_open)):
        try: age_ms = int(snapshot.get("timestamp") or 0) * 1000 - int(status.get("timestamp_ms") or 0)
        except (TypeError, ValueError, OverflowError): age_ms = (max_runtime_age + 1) * 1000
        if age_ms < -5_000 or age_ms > max_runtime_age * 1000: reasons.append(f"{name}_live_collector_stale")
    try: osint_mapping_age_ms = int(snapshot.get("timestamp") or 0) * 1000 - int(osint_mapping.get("timestamp_ms") or 0)
    except (TypeError, ValueError, OverflowError): osint_mapping_age_ms = (max_runtime_age + 1) * 1000
    if osint_mapping_age_ms < -5_000 or osint_mapping_age_ms > max_runtime_age * 1000:
        reasons.append("osint_mapping_pipeline_stale")
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
    if external_fair.get("external_fair_required_markets", 0) and not external_fair.get("shadow_zero_authority", True):
        reasons.extend(str(reason) for reason in external_fair.get("hard_reasons", []))
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
    research = snapshot.get("research_sleeves") if isinstance(snapshot.get("research_sleeves"), dict) else {}
    research_statuses = snapshot.get("research_shadow_statuses") if isinstance(snapshot.get("research_shadow_statuses"), dict) else {}
    registry = snapshot.get("strategy_registry") if isinstance(snapshot.get("strategy_registry"), dict) else {}
    live_scope = snapshot.get("live_model_scope") if isinstance(snapshot.get("live_model_scope"), dict) else {}
    research_rows = research.get("families") if isinstance(research.get("families"), dict) else {}
    enabled_registry = [row for row in registry.get("strategies", []) if isinstance(row, dict) and row.get("enabled") is True] if isinstance(registry.get("strategies"), list) else []
    expected_research = {"sports_latency", "cross_platform", "wallet_intelligence"}
    target_live = set(live_scope.get("target_live_families") or [])
    excluded_live = set(live_scope.get("excluded_live_families") or [])
    enabled_names = {str(row.get("family") or "") for row in enabled_registry}
    governance = live_scope.get("governance") if isinstance(live_scope.get("governance"), dict) else {}
    scope_valid = (
        live_scope.get("schema") == "polymarket_v7_live_model_scope_v1"
        and live_scope.get("target_live_count") == 12
        and live_scope.get("paper_only") is True
        and live_scope.get("authenticated_execution") is False
        and live_scope.get("real_order_submission") is False
        and len(target_live) == 12
        and target_live | excluded_live == enabled_names
        and not target_live & excluded_live
        and excluded_live == {"ranking", "pca", "local_factor"}
        and set(live_scope.get("research_shadow_supervised_families") or []) == expected_research
        and governance.get("single_execution_owner") is True
        and governance.get("research_has_capital") is False
        and governance.get("research_has_oms_authority") is False
        and governance.get("research_has_ledger_writer_authority") is False
        and governance.get("automatic_promotion") is False
    )
    def research_row_valid(family: str, row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        base = (
            row.get("authority") == "RESEARCH"
            and row.get("paper_only") is True
            and row.get("authenticated_execution") is False
            and row.get("real_order_submission") is False
            and row.get("process_state") == "RUNNING"
            and all(row.get(key) is False for key in ("execution_authority", "capital_authority", "oms_authority", "ledger_write_authority", "promotion_authority"))
            and tuple(Path(str(row.get("status_path") or "")).parts[-3:]) == ("shadow", family, "status.json")
            and tuple(Path(str(row.get("output_path") or "")).parts[-2:]) == ("shadow", family)
        )
        if not base:
            return False
        if family == "wallet_intelligence":
            return row.get("evidence_state") == "BLOCKED_CONFIG" and row.get("last_attempt_ts") == 0 and row.get("last_success_ts") == 0
        state = row.get("evidence_state")
        return (
            state in {"BLOCKED_EXTERNAL", "ACTIVE"}
            and row.get("implementation_complete") is True
            and _integer(row.get("last_attempt_ts")) > 0
            and (state != "ACTIVE" or (
                row.get("feed_operational") is True
                and _integer(row.get("verified_mappings")) > 0
                and row.get("forward_collection_active") is True
            ))
            and (state != "BLOCKED_EXTERNAL" or bool(str(row.get("blocker") or "")))
        )
    research_rows_valid = set(research_rows) == expected_research and all(
        research_row_valid(family, row) for family, row in research_rows.items()
    )
    def collector_contract_valid(status: Any) -> bool:
        return (
            isinstance(status, dict)
            and status.get("paper_only") is True
            and status.get("authenticated_execution") is False
            and status.get("real_order_submission") is False
        )
    collector_contracts_valid = all(
        collector_contract_valid(snapshot.get(name)) for name in ("osint", "market_open")
    )
    try: research_manifest_age = int(snapshot.get("timestamp") or 0) - int(research.get("timestamp") or 0)
    except (TypeError, ValueError, OverflowError): research_manifest_age = math.inf
    research_manifest_fresh = -5 <= research_manifest_age <= 180
    def shadow_status_valid(family: str, status: Any) -> bool:
        if not isinstance(status, dict):
            return False
        try:
            age = int(snapshot.get("timestamp") or 0) - int(status.get("timestamp") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            status.get("schema") == "polymarket_v7_research_shadow_status_v1"
            and status.get("family") == family
            and status.get("model_sha") == snapshot.get("sha")
            and status.get("paper_only") is True
            and status.get("authenticated_execution") is False
            and status.get("real_order_submission") is False
            and status.get("process_state") == "RUNNING"
            and status.get("evidence_state") == (research_rows.get(family) or {}).get("evidence_state")
            and research_row_valid(family, status)
            and -5 <= age <= 180
        )
    research_statuses_valid = set(research_statuses) == expected_research and all(
        shadow_status_valid(family, status) for family, status in research_statuses.items()
    )
    research_attached = (
        research.get("schema") == "polymarket_v7_research_sleeves_manifest_v1"
        and research.get("model_sha") == snapshot.get("sha")
        and research.get("paper_only") is True
        and research.get("authenticated_execution") is False
        and research.get("real_order_submission") is False
        and research_rows_valid
        and research_statuses_valid
        and _pid_alive(research.get("supervisor_pid"))
        and research_manifest_fresh
    )
    target_live_count = len(set(live_scope.get("target_live_families") or []))
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
    osint_mapping = snapshot.get("osint_mapping") if isinstance(snapshot.get("osint_mapping"), dict) else {}
    market_open = snapshot.get("market_open") if isinstance(snapshot.get("market_open"), dict) else {}
    universe = snapshot.get("universe") if isinstance(snapshot.get("universe"), dict) else {}
    maker = snapshot.get("maker") if isinstance(snapshot.get("maker"), dict) else {}
    maker_diagnostics = snapshot.get("maker_diagnostics") if isinstance(snapshot.get("maker_diagnostics"), dict) else {}
    selector = snapshot.get("maker_selector") if isinstance(snapshot.get("maker_selector"), dict) else {}
    def collector_fresh(status: dict[str, Any]) -> bool:
        try:
            age_ms = int(snapshot.get("timestamp") or 0) * 1000 - int(status.get("timestamp_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        return -5_000 <= age_ms <= 180_000
    osint_operational = (
        osint.get("schema") == "polymarket_v7_osint_collector_status_v1"
        and collector_fresh(osint)
        and collector_contract_valid(osint)
        and _integer(osint.get("enabled_sources")) > 0
        and _integer(osint.get("healthy_sources")) > 0
        and osint_mapping.get("schema") == "polymarket_v7_osint_mapping_status_v1"
        and _integer(osint_mapping.get("verified_mappings")) > 0
        and osint_mapping.get("forward_collection_active") is True
    )
    market_open_operational = (
        market_open.get("schema") == "polymarket_v7_market_open_collector_status_v1"
        and collector_fresh(market_open)
        and collector_contract_valid(market_open)
        and _integer(market_open.get("observed_markets")) > 0
    )
    def fresh_milliseconds(status: dict[str, Any]) -> bool:
        try:
            age = int(snapshot.get("timestamp") or 0) * 1000 - int(status.get("timestamp_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        return -5_000 <= age <= 180_000
    selector_ready = (
        selector.get("schema") == "polymarket_v7_maker_selector_status_v1"
        and selector.get("model_sha") == snapshot.get("sha")
        and selector.get("paper_only") is True
        and selector.get("authenticated_execution") is False
        and selector.get("real_order_submission") is False
        and selector.get("ready") is True
        and selector.get("state") in _MAKER_SELECTOR_OPERATIONAL_STATES
        and fresh_milliseconds(selector)
    )
    maker_operational = (
        maker.get("schema") == "polymarket_v7_professional_maker_status_v1"
        and maker.get("model_sha") == snapshot.get("sha")
        and maker.get("paper_only") is True
        and maker.get("authenticated_execution") is False
        and maker.get("killed") is not True
        and maker.get("new_risk_frozen") is not True
        and maker.get("source") not in {None, "", "not_started"}
        and fresh_milliseconds(maker)
        and selector_ready
    )
    blocked_config_count = sum(
        1 for row in research_rows.values()
        if isinstance(row, dict) and row.get("evidence_state") == "BLOCKED_CONFIG"
    )
    blocked_external_count = sum(
        1 for row in research_rows.values()
        if isinstance(row, dict) and row.get("evidence_state") == "BLOCKED_EXTERNAL"
    ) + (0 if osint_operational else 1)
    active_research_count = sum(
        1 for row in research_rows.values()
        if isinstance(row, dict) and row.get("evidence_state") == "ACTIVE"
    )
    operational_count = (
        (7 if snapshot.get("runtime_alive") is True else 0)
        + (1 if osint_operational else 0)
        + (1 if market_open_operational else 0)
        + active_research_count
    )
    scope_wired = (
        len(enabled_registry) == 15 and target_live_count == 12 and scope_valid
        and research_attached and collector_contracts_valid
        and snapshot.get("runtime_alive") is True
    )
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
        _metric("polymarket_v7_component_ready", 1 if maker_operational else 0, {"component": "professional_maker"}),
        _metric("polymarket_v7_maker_selector_ready", 1 if selector_ready else 0),
        _metric("polymarket_v7_maker_selector_fallback_active", 1 if selector_ready and selector.get("degraded") is True else 0),
        _metric("polymarket_v7_maker_selector_selected_markets", selector.get("selected_count")),
        _metric("polymarket_v7_maker_runtime_selection_pinned", 1 if selector.get("runtime_selection_pinned") is True else 0),
        _metric("polymarket_v7_maker_candidate_rotation_pending", 1 if selector.get("candidate_rotation_pending") is True else 0),
        _metric("polymarket_v7_maker_candidate_selected_markets", selector.get("candidate_selected_count")),
        _metric("polymarket_v7_maker_new_risk_frozen", 1 if maker.get("new_risk_frozen") is True else 0),
        _metric("polymarket_v7_maker_marking_complete", 1 if maker.get("marking_complete") is True else 0),
        _metric("polymarket_v7_maker_feed_workers", maker_diagnostics.get("feed_workers")),
        _metric("polymarket_v7_maker_feed_connected_workers", maker_diagnostics.get("feed_connected_workers")),
        _metric("polymarket_v7_maker_feed_messages_total", maker_diagnostics.get("feed_messages")),
        _metric("polymarket_v7_maker_feed_reconnects_total", maker_diagnostics.get("feed_reconnects")),
        _metric("polymarket_v7_maker_feed_errors_total", maker_diagnostics.get("feed_errors")),
        _metric("polymarket_v7_maker_decisions_total", maker_diagnostics.get("decisions")),
        _metric("polymarket_v7_maker_quote_intents_total", maker_diagnostics.get("quote_intents")),
        _metric("polymarket_v7_maker_rejected_nonpositive_robust_ev_total", maker_diagnostics.get("rejected_nonpositive_robust_ev")),
        _metric("polymarket_v7_maker_selector_info", 1 if selector_ready else 0, {
            "state": selector.get("state", "UNKNOWN"),
            "source": selector.get("source", "UNKNOWN"),
        }),
        _metric("polymarket_v7_supervisor_alive", 1 if operations.get("supervisor_alive") else 0),
        _metric("polymarket_v7_monitoring_component_up", 1, {"component": "exporter"}),
        _metric("polymarket_v7_monitoring_component_up", 1 if operations.get("grafana_up") else 0, {"component": "grafana"}),
        _metric("polymarket_v7_supervisor_info", 1, {"state": supervisor.get("state", "UNKNOWN")}),
        _metric("polymarket_v7_runtime_uptime_seconds", operations.get("runtime_uptime")),
        _metric("polymarket_v7_restart_count_window", operations.get("restart_count")),
        _metric("polymarket_v7_single_writer_ok", 1 if operations.get("single_writer") else 0),
        _metric("polymarket_v7_exact_sha_ok", 1 if runtime.get("model_sha") == snapshot["sha"] else 0),
        _metric("polymarket_v7_strategy_registry_enabled", len(enabled_registry)),
        _metric("polymarket_v7_research_sleeves_attached", len(research_rows)),
        _metric("polymarket_v7_research_supervisor_alive", 1 if _pid_alive(research.get("supervisor_pid")) else 0),
        _metric("polymarket_v7_research_manifest_fresh", 1 if research_manifest_fresh else 0),
        _metric("polymarket_v7_live_model_target_count", target_live_count),
        _metric("polymarket_v7_live_model_operational_count", operational_count),
        _metric("polymarket_v7_live_model_blocked_count", blocked_config_count + blocked_external_count),
        _metric("polymarket_v7_live_model_blocked_config_count", blocked_config_count),
        _metric("polymarket_v7_live_model_blocked_external_count", blocked_external_count),
        _metric("polymarket_v7_live_model_scope_wired", 1 if scope_wired else 0),
        _metric("polymarket_v7_live_model_target_operational", 1 if operational_count == target_live_count == 12 else 0),
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
        _metric("polymarket_v7_osint_mapping_verified", osint_mapping.get("verified_mappings")),
        _metric("polymarket_v7_osint_mapping_candidates", osint_mapping.get("candidate_mappings")),
        _metric("polymarket_v7_osint_forward_collection_active", 1 if osint_mapping.get("forward_collection_active") is True else 0),
        _metric("polymarket_v7_market_open_tracked_markets", market_open.get("tracked_markets")),
        _metric("polymarket_v7_market_open_new_markets", market_open.get("new_markets")),
        _metric("polymarket_v7_market_open_emitted_milestones", market_open.get("emitted_milestones")),
        _metric("polymarket_v7_market_open_semantic_verified", market_open.get("semantic_verified_markets")),
        _metric("polymarket_v7_universe_discovery_exhaustive", 1 if universe.get("discovery_exhaustive") is True else 0),
        _metric("polymarket_v7_universe_discovered_markets", universe.get("discovered_markets")),
        _metric("polymarket_v7_universe_eligible_markets", universe.get("eligible_markets")),
        _metric("polymarket_v7_universe_skipped_markets", universe.get("skipped_markets")),
        _metric("polymarket_v7_universe_scan_duration_milliseconds", universe.get("scan_duration_ms")),
        _metric("polymarket_v7_universe_pages", universe.get("pages")),
        _metric("polymarket_v7_universe_request_retries", universe.get("request_retries")),
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
    for reason, count in sorted((maker_diagnostics.get("reason_counts") or {}).items()):
        lines.append(_metric("polymarket_v7_maker_decision_reason_total", count, {"reason": reason}))
    for tier, count in sorted((universe.get("tier_counts") or {}).items()):
        lines.append(_metric("polymarket_v7_universe_tier_markets", count, {"tier": tier}))
    for reason, count in sorted((universe.get("skipped_by_reason") or {}).items()):
        lines.append(_metric("polymarket_v7_universe_skipped_by_reason", count, {"reason": reason}))
    capacities = universe.get("resource_capacities") if isinstance(universe.get("resource_capacities"), dict) else {}
    for dimension in capacities.get("hot_limiting_dimensions") or []:
        lines.append(_metric("polymarket_v7_universe_resource_limit", 1, {"tier": "HOT", "dimension": dimension}))
    for dimension in capacities.get("warm_limiting_dimensions") or []:
        lines.append(_metric("polymarket_v7_universe_resource_limit", 1, {"tier": "WARM", "dimension": dimension}))
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
    for family, row in sorted(research_rows.items()):
        if not isinstance(row, dict):
            continue
        labels_r = {
            "strategy": family,
            "process_state": row.get("process_state", "UNKNOWN"),
            "evidence_state": row.get("evidence_state", "UNKNOWN"),
        }
        lines.append(_metric("polymarket_v7_research_sleeve_attached", 1, labels_r))
        blocker_labels = {
            "strategy": family,
            "feed_status": row.get("feed_status", "NOT_CONFIGURED"),
            "mapping_status": row.get("mapping_status", "NOT_CONFIGURED"),
            "blocker": row.get("blocker", "") or "NONE",
        }
        lines.append(_metric("polymarket_v7_external_input_info", 1, blocker_labels))
        lines.append(_metric("polymarket_v7_external_input_implementation_complete",
                             1 if row.get("implementation_complete") is True else 0,
                             {"strategy": family}))
        lines.append(_metric("polymarket_v7_external_input_feed_operational",
                             1 if row.get("feed_operational") is True else 0,
                             {"strategy": family}))
        lines.append(_metric("polymarket_v7_external_input_verified_mappings",
                             row.get("verified_mappings"), {"strategy": family}))
        lines.append(_metric("polymarket_v7_external_input_forward_collection_active",
                             1 if row.get("forward_collection_active") is True else 0,
                             {"strategy": family}))
        for key, metric_name in (
            ("feed_age_ms", "polymarket_v7_external_input_feed_age_milliseconds"),
            ("last_sequence", "polymarket_v7_external_input_last_sequence"),
            ("connection_epoch", "polymarket_v7_external_input_connection_epoch"),
            ("reconnect_count", "polymarket_v7_external_input_reconnect_total"),
            ("gap_count", "polymarket_v7_external_input_gap_total"),
            ("parse_failure_count", "polymarket_v7_external_input_parse_failure_total"),
            ("dropped_event_count", "polymarket_v7_external_input_dropped_event_total"),
        ):
            if row.get(key) is not None:
                lines.append(_metric(metric_name, row.get(key), {"strategy": family}))
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
    # These appenders contain metrics only; the canonical exporter remains the
    # sole HTTP listener and snapshot owner.
    from exporter_v7_fillability import _append_fillability_metrics
    from exporter_v7_external import _append_external_fair_metrics
    _append_fillability_metrics(lines, snapshot.get("maker_fillability") or {})
    _append_external_fair_metrics(lines, snapshot.get("external_fair") or {})
    return "\n".join(lines)+"\n"


class SnapshotCache:
    """Refresh expensive ledger diagnostics off the HTTP request path.

    The canonical ledger is append-only and can grow by thousands of records a
    minute.  Re-reading it for every Prometheus scrape eventually exceeds the
    scrape timeout and creates a request stampede.  One worker owns snapshot
    construction; readers always receive the most recent complete snapshot.
    """

    def __init__(
        self,
        run_root: Path,
        repository_root: Path,
        *,
        refresh_seconds: float = 10.0,
    ) -> None:
        self.run_root = Path(run_root)
        self.repository_root = Path(repository_root)
        self.refresh_seconds = max(1.0, float(refresh_seconds))
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] | None = None
        self._metrics = b""
        self._maker_fillability = b"{}\n"
        self._external_fair = b"{}\n"
        self._completed_monotonic = 0.0
        self._completed_wall = 0.0
        self._refresh_duration = 0.0
        self._refresh_errors = 0
        self._last_error = ""

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._refresh_loop,
            name="v7-exporter-snapshot",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.refresh_seconds + 1.0))

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snapshot = collect_snapshot(self.run_root, self.repository_root)
                duration = time.monotonic() - started
                completed_wall = time.time()
                metrics_text = render_prometheus(snapshot).rstrip("\n")
                metrics_text += (
                    "\n"
                    f"polymarket_v7_exporter_snapshot_generated_unixtime {completed_wall}\n"
                    f"polymarket_v7_exporter_snapshot_refresh_duration_seconds {duration}\n"
                    f"polymarket_v7_exporter_snapshot_refresh_errors_total {self._refresh_errors}\n"
                )
                maker = snapshot.get("maker_fillability")
                external = snapshot.get("external_fair")
                with self._lock:
                    self._snapshot = snapshot
                    self._metrics = metrics_text.encode()
                    self._maker_fillability = (
                        json.dumps(maker if isinstance(maker, dict) else {}, sort_keys=True) + "\n"
                    ).encode()
                    self._external_fair = (
                        json.dumps(external if isinstance(external, dict) else {}, sort_keys=True) + "\n"
                    ).encode()
                    self._completed_monotonic = time.monotonic()
                    self._completed_wall = completed_wall
                    self._refresh_duration = duration
                    self._last_error = ""
                self._ready.set()
            except Exception as exc:  # pragma: no cover - defensive service boundary
                with self._lock:
                    self._refresh_errors += 1
                    self._last_error = f"{type(exc).__name__}:{exc}"
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.1, self.refresh_seconds - elapsed))

    def read(self) -> dict[str, Any]:
        with self._lock:
            age = (
                max(0.0, time.monotonic() - self._completed_monotonic)
                if self._completed_monotonic > 0.0
                else math.inf
            )
            return {
                "ready": self._snapshot is not None,
                "snapshot": self._snapshot,
                "metrics": self._metrics,
                "maker_fillability": self._maker_fillability,
                "external_fair": self._external_fair,
                "age_seconds": age,
                "completed_wall": self._completed_wall,
                "refresh_duration_seconds": self._refresh_duration,
                "refresh_errors": self._refresh_errors,
                "last_error": self._last_error,
            }


class ExporterHandler(BaseHTTPRequestHandler):
    run_root=Path("runs/paper_v7_live"); repository_root=Path("."); max_runtime_age=180; max_supervisor_age=30; max_snapshot_age=45.0
    snapshot_cache: SnapshotCache | None = None
    def log_message(self,_format:str,*_args:object)->None: return
    def do_GET(self)->None:  # noqa: N802
        cached = self.snapshot_cache.read() if self.snapshot_cache is not None else None
        if cached is None:
            snapshot=collect_snapshot(self.run_root,self.repository_root)
            cached={
                "ready": True,
                "snapshot": snapshot,
                "metrics": render_prometheus(snapshot).encode(),
                "maker_fillability": (json.dumps(snapshot.get("maker_fillability") or {},sort_keys=True)+"\n").encode(),
                "external_fair": (json.dumps(snapshot.get("external_fair") or {},sort_keys=True)+"\n").encode(),
                "age_seconds": 0.0,
                "last_error": "",
            }
        if not cached.get("ready"):
            payload=(json.dumps({"ok":False,"reasons":["exporter_snapshot_not_ready"]},sort_keys=True)+"\n").encode()
            self.send_response(503); self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Retry-After","1")
            self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        snapshot=cached["snapshot"]
        if self.path=="/metrics": payload=cached["metrics"]; self.send_response(200); self.send_header("Content-Type","text/plain; version=0.0.4; charset=utf-8")
        elif self.path=="/healthz":
            reasons=health_reasons(snapshot,max_runtime_age=self.max_runtime_age,max_supervisor_age=self.max_supervisor_age)
            snapshot_age = cached.get("age_seconds")
            if not isinstance(snapshot_age, (int, float)) or float(snapshot_age) > self.max_snapshot_age: reasons.append("exporter_snapshot_stale")
            reasons=sorted(set(reasons)); payload=(json.dumps({"ok":not reasons,"reasons":reasons},sort_keys=True)+"\n").encode(); self.send_response(200 if not reasons else 503); self.send_header("Content-Type","application/json; charset=utf-8")
        elif self.path=="/maker-fillability.json": payload=cached["maker_fillability"]; self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8")
        elif self.path=="/external-fair.json": payload=cached["external_fair"]; self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8")
        else: payload=b"not found\n"; self.send_response(404); self.send_header("Content-Type","text/plain; charset=utf-8")
        self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--run-root",type=Path,default=Path("runs/paper_v7_live")); parser.add_argument("--repository-root",type=Path,default=Path(".")); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=9108); parser.add_argument("--max-runtime-age",type=int,default=180); parser.add_argument("--max-supervisor-age",type=int,default=30); parser.add_argument("--snapshot-refresh-seconds",type=float,default=10.0); parser.add_argument("--max-snapshot-age",type=float,default=45.0); args=parser.parse_args()
    ExporterHandler.run_root=args.run_root; ExporterHandler.repository_root=args.repository_root; ExporterHandler.max_runtime_age=max(1,args.max_runtime_age); ExporterHandler.max_supervisor_age=max(1,args.max_supervisor_age); ExporterHandler.max_snapshot_age=max(5.0,args.max_snapshot_age)
    cache=SnapshotCache(args.run_root,args.repository_root,refresh_seconds=args.snapshot_refresh_seconds); cache.start(); ExporterHandler.snapshot_cache=cache
    server=ThreadingHTTPServer((args.host,args.port),ExporterHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); cache.stop()
    return 0

if __name__=="__main__": raise SystemExit(main())
