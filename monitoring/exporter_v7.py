#!/usr/bin/env python3
"""Prometheus exporter for the canonical two-engine V7 PAPER runtime."""
from __future__ import annotations

import argparse, csv, hashlib, json, math, os, shutil, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from v7_external_fair import summarize_external_fair
from v7_ledger_metrics import summarize_ledger
from v7_maker_fillability_exact import summarize_best_available_fillability
from v7_maker_microstructure import summarize_maker_microstructure
from v7_portfolio_reconciliation import reconcile as reconcile_portfolio
from v7_runtime_contract import (
    MAKER_ROTATION_OPERATIONAL_STATES as _MAKER_ROTATION_OPERATIONAL_STATES,
    MAKER_SELECTOR_OPERATIONAL_STATES as _MAKER_SELECTOR_OPERATIONAL_STATES,
)

LIVE_ALGORITHMS = ("CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE")
_FILLABILITY_CACHE_KEY: tuple[str, str, int] | None = None
_FILLABILITY_CACHE_VALUE: dict[str, Any] | None = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _integer(value: Any, default: int = 0) -> int:
    return int(_number(value, default))


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _age(now: int, timestamp: Any) -> float:
    value = _number(timestamp)
    return math.inf if value <= 0 else max(0.0, now - value)


def _file_age(path: Path, now: int) -> float:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return math.inf


def _git_head(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL, timeout=2,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _pid_alive(value: Any) -> bool:
    pid = _integer(value)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def _safe_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _metric(name: str, value: Any, labels: dict[str, Any] | None = None) -> str:
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f'{k}="{_safe_label(v)}"' for k, v in labels.items()) + "}"
    return f"{name}{suffix} {_number(value):.12g}"


def _trade_tape(path: Path, now: int) -> dict[str, Any]:
    rows, newest = 0, 0
    assets: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                if row.get("asset_id"):
                    assets.add(str(row["asset_id"]))
                newest = max(newest, _integer(row.get("received_ms")))
    except (OSError, csv.Error):
        pass
    return {"rows": rows, "assets": len(assets), "age": _age(now, newest / 1000 if newest else 0)}


def _trade_recorder(path: Path, now: int) -> dict[str, Any]:
    status = _json(path)
    return {**status, "present": bool(status), "age": _age(now, _integer(status.get("timestamp_ms")) / 1000)}


def _verified_no_flow(status: dict[str, Any], max_age: float) -> bool:
    return (
        status.get("schema") == "polymarket_v7_trade_recorder_status_v1"
        and status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and status.get("data_plane_healthy") is True
        and status.get("flow_regime") == "STANDARD_CLOB_NO_MATCHING_TRADES"
        and _integer(status.get("conditions")) > 0 and _integer(status.get("requests")) > 0
        and _integer(status.get("fetched")) == 0 and _integer(status.get("errors")) == 0
        and _integer(status.get("truncated_batches")) == 0
        and _number(status.get("age"), math.inf) <= max_age
    )


def _maker_latency(path: Path) -> dict[str, Any]:
    names = ("parse_ns", "book_ns", "feature_ns", "decision_ns", "risk_ns", "tx_queue_ns", "execution_ns", "receive_to_intent_ns")
    values: dict[str, list[int]] = {name: [] for name in names}
    rows = 0
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                for name in names:
                    value = _integer(row.get(name))
                    if value > 0:
                        values[name].append(value)
    except (OSError, csv.Error):
        return {"present": False, "rows": 0, "stages": {}}
    stages: dict[str, Any] = {}
    for name, samples in values.items():
        if not samples:
            continue
        samples.sort()
        pick = lambda p: samples[int(p * (len(samples) - 1))]
        stages[name] = {"samples": len(samples), "p50": pick(.5), "p90": pick(.9), "p95": pick(.95), "p99": pick(.99), "p99_9": pick(.999), "max": samples[-1]}
    return {"present": rows > 0, "rows": rows, "stages": stages}


def _fillability(run_root: Path, repository_root: Path, sha: str, now: int) -> dict[str, Any]:
    global _FILLABILITY_CACHE_KEY, _FILLABILITY_CACHE_VALUE
    key = (str(run_root), sha, now // 30)
    if key == _FILLABILITY_CACHE_KEY and _FILLABILITY_CACHE_VALUE is not None:
        return _FILLABILITY_CACHE_VALUE
    value = summarize_best_available_fillability(
        run_root / "ledger/execution.jsonl", run_root / "trade_tape.csv",
        repository_root / "config/v7_professional_market_maker.json",
        model_sha=sha or None, now_ms=now * 1000,
    )
    _FILLABILITY_CACHE_KEY, _FILLABILITY_CACHE_VALUE = key, value
    return value


def _operations(run_root: Path, runtime: dict[str, Any], now: int) -> dict[str, Any]:
    supervisor = _json(run_root / "control/supervisor_status.json")
    retention = _json(run_root / "control/retention_status.json")
    try:
        lock_pid = int((run_root / "control/runtime.lock/pid").read_text().strip())
    except (OSError, ValueError):
        lock_pid = 0
    runtime_pid = _integer(runtime.get("pid"))
    child_pid = _integer(supervisor.get("child_pid"))
    try:
        free_ratio = shutil.disk_usage(run_root).free / shutil.disk_usage(run_root).total
    except OSError:
        free_ratio = 0.0
    return {
        "supervisor": supervisor, "retention": retention,
        "supervisor_alive": _pid_alive(supervisor.get("supervisor_pid")),
        "single_writer": runtime_pid > 0 and runtime_pid == lock_pid and _pid_alive(lock_pid) and child_pid in {0, runtime_pid},
        "runtime_uptime": max(0, now - _integer(supervisor.get("started_at"))),
        "restart_count": _integer(supervisor.get("restart_count_window")),
        "ledger_writable": os.access(run_root / "ledger", os.W_OK),
        "disk_free_ratio": free_ratio, "retention_age": _age(now, retention.get("timestamp")),
    }


def collect_snapshot(run_root: Path, repository_root: Path | None = None, *, now: int | None = None) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    run_root = run_root.resolve(); repository_root = (repository_root or Path(".")).resolve()
    runtime = _json(run_root / "control/runtime_status.json")
    portfolio = _json(run_root / "control/portfolio_state.json")
    allocations = _json(run_root / "control/allocations/manifest.json")
    canonical_path = run_root / "canonical_economics.json"
    canonical = _json(canonical_path)
    ledger_path = run_root / "ledger/execution.jsonl"
    ledger = summarize_ledger(ledger_path)
    fast = _json(run_root / "fast_structural/fast_arb_status.json")
    hard = _json(run_root / "hard_arb/status.json")
    maker = _json(run_root / "micro_maker/status.json")
    sha, runtime_sha = _git_head(repository_root), str(runtime.get("model_sha") or "")
    directives = _json(repository_root / "config/operator_directives.json")
    authorization = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    max_drawdown = _number(authorization.get("max_drawdown"))
    authority_valid = directives.get("authority") == "latest_explicit_user_instruction" and authorization.get("paper_only") is True and authorization.get("authenticated_execution") is False and 0 < max_drawdown <= 1
    process_path = repository_root / "config/v7_process_manifest.json"
    process = _json(process_path)
    external_fair = summarize_external_fair(run_root, repository_root, runtime_sha=runtime_sha, now_s=now)
    state_pnl = {
        "CRYPTO_SETTLEMENT_ENGINE": _number((external_fair.get("economics") or {}).get("realized_pnl")),
        "STRUCTURAL_ARB_ENGINE": _number(hard.get("realized_pnl_total")),
    }
    reconciliation = reconcile_portfolio(canonical=canonical, ledger=ledger, portfolio=portfolio, allocations=allocations, state_realized_pnl=state_pnl)
    engine_rows = portfolio.get("engines") if isinstance(portfolio.get("engines"), dict) else {}
    algorithms = {engine: {"equity": _number((engine_rows.get(engine) or {}).get("equity")), "budget": _number((engine_rows.get(engine) or {}).get("budget")), "killed": bool((engine_rows.get(engine) or {}).get("killed"))} for engine in LIVE_ALGORITHMS}
    tape = _trade_tape(run_root / "trade_tape.csv", now)
    starting = _number(portfolio.get("account_starting_capital"), _number(allocations.get("account_starting_capital")))
    equity = _number(portfolio.get("equity"), starting)
    return {
        "timestamp": now, "sha": sha, "run_root": run_root.name, "runtime": runtime,
        "runtime_alive": _pid_alive(runtime.get("pid")) and not (run_root / "control/KILL").exists(),
        "portfolio": portfolio, "allocations": allocations,
        "evidence_allocator": _json(run_root / "control/evidence_capital_allocator.json"),
        "fee_reward_registry": _json(run_root / "control/fee_reward_registry.json"),
        "strategy_registry": _json(repository_root / "config/v7_strategy_registry.json"),
        "live_model_scope": _json(repository_root / "config/v7_live_model_scope.json"),
        "crypto_registry": _json(repository_root / "config/v7_crypto_settlement_markets.json"),
        "crypto_model_registry": _json(repository_root / "config/v7_crypto_settlement_model_registry.json"),
        "crypto_runtime": _json(run_root / "control/crypto_settlement_engine_snapshot.json"),
        "global_coordinator": _json(run_root / "control/global_portfolio_coordinator.json"),
        "process_manifest": {"schema": process.get("schema"), "process_count": len(process.get("processes") or []), "sha256": hashlib.sha256(process_path.read_bytes()).hexdigest() if process_path.exists() else ""},
        "fast": fast, "hard": hard, "maker": maker,
        "maker_diagnostics": _json(run_root / "micro_maker/runtime_diagnostics.json"),
        "maker_selector": _json(run_root / "micro_maker/selector_status.json"),
        "maker_rotation": _json(run_root / "micro_maker/rotation_status.json"),
        "external": _json(run_root / "external/status.json"),
        "universe": _json(run_root / "universe/status.json"),
        "canonical_economics": canonical, "ledger": ledger,
        "maker_lab": summarize_maker_microstructure(ledger_path, run_root / "micro_maker/reward_selection.json", run_root / "research/evidence/maker_markout"),
        "maker_fillability": _fillability(run_root, repository_root, runtime_sha, now),
        "external_fair": external_fair, "reconciliation": reconciliation,
        "maker_latency": _maker_latency(run_root / "micro_maker/latency.csv"),
        "trade_tape": tape, "trade_recorder": _trade_recorder(run_root / "trade_recorder_status.json", now),
        "authority": {"valid": authority_valid, "max_drawdown": max_drawdown},
        "algorithms": algorithms, "strategies": algorithms,
        "ages": {"runtime": _age(now, runtime.get("timestamp")), "portfolio": _age(now, portfolio.get("timestamp")), "economics": _file_age(canonical_path, now), "trade_tape": tape["age"]},
        "operations": _operations(run_root, runtime, now),
        "economics": {"starting_capital": starting, "cash": _number(allocations.get("reserve_budget")), "equity": equity, "pnl": equity-starting, "realized_pnl": _number(canonical.get("net_pnl")), "unrealized_executable_pnl": equity-starting-_number(canonical.get("net_pnl")), "drawdown": _number(portfolio.get("drawdown")), "gross_exposure": 0.0, "capital_utilization": 0.0, "live_units": 0, "killed": bool(portfolio.get("killed"))},
    }


def _fresh_ms(status: dict[str, Any], snapshot: dict[str, Any], max_age: int) -> bool:
    age = _integer(snapshot.get("timestamp")) * 1000 - _integer(status.get("timestamp_ms"))
    return -5000 <= age <= max_age * 1000


def _scope_valid(snapshot: dict[str, Any]) -> bool:
    scope, registry = snapshot.get("live_model_scope") or {}, snapshot.get("strategy_registry") or {}
    rows = registry.get("live_algorithms") if isinstance(registry.get("live_algorithms"), list) else []
    ids = [row.get("id") for row in rows if isinstance(row, dict) and row.get("enabled") is True]
    return scope.get("schema") == "polymarket_v7_live_engine_scope_v2" and scope.get("live_algorithm_count") == 2 and set(scope.get("live_algorithms") or []) == set(LIVE_ALGORITHMS) and registry.get("schema") == "polymarket_v7_live_algorithm_registry_v2" and len(ids) == 2 and set(ids) == set(LIVE_ALGORITHMS) and scope.get("component_independent_authority") is False and registry.get("component_independent_authority") is False


def health_reasons(snapshot: dict[str, Any], *, max_runtime_age: int = 180, max_supervisor_age: int = 30) -> list[str]:
    reasons: list[str] = []
    runtime, portfolio, allocations = snapshot.get("runtime") or {}, snapshot.get("portfolio") or {}, snapshot.get("allocations") or {}
    canonical, ledger, ages = snapshot.get("canonical_economics") or {}, snapshot.get("ledger") or {}, snapshot.get("ages") or {}
    fast, maker = snapshot.get("fast") or {}, snapshot.get("maker") or {}
    selector, rotation, universe = snapshot.get("maker_selector") or {}, snapshot.get("maker_rotation") or {}, snapshot.get("universe") or {}
    if not (snapshot.get("authority") or {}).get("valid"): reasons.append("operator_authority_missing_or_invalid")
    if runtime.get("version") != 7: reasons.append("runtime_version_not_v7")
    if runtime.get("paper_only") is not True: reasons.append("runtime_not_paper_only")
    if runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False: reasons.append("authenticated_execution_not_disabled")
    if runtime.get("model_sha") != snapshot.get("sha"): reasons.append("runtime_sha_mismatch")
    if set(runtime.get("economic_engines") or []) != set(LIVE_ALGORITHMS): reasons.append("runtime_live_algorithms_not_exactly_two")
    if runtime.get("economic_new_risk_ready") is not False: reasons.append("economic_new_risk_must_remain_disabled")
    if runtime.get("authorized_alpha_actions") not in (None, []): reasons.append("authorized_alpha_actions_not_empty")
    if not _scope_valid(snapshot): reasons.append("live_algorithm_scope_missing_or_invalid")
    if (snapshot.get("process_manifest") or {}).get("process_count") != 22: reasons.append("process_manifest_not_22_exact")
    budgets = allocations.get("engine_budgets") if isinstance(allocations.get("engine_budgets"), dict) else {}
    if allocations.get("schema") != "polymarket_v7_capital_allocation_v3" or set(budgets) != set(LIVE_ALGORITHMS) or allocations.get("engine_count") != 2 or allocations.get("paper_only") is not True or allocations.get("authenticated_execution") is not False or allocations.get("real_order_submission") is not False or allocations.get("real_capital_at_risk") is not False or allocations.get("capital_authority_owner_count") != 1: reasons.append("two_engine_allocation_missing_or_unsafe")
    engines = portfolio.get("engines") if isinstance(portfolio.get("engines"), dict) else {}
    if portfolio.get("schema") != "polymarket_v7_portfolio_guard_v2" or set(engines) != set(LIVE_ALGORITHMS) or portfolio.get("paper_only") is not True or portfolio.get("authenticated_execution") is not False or portfolio.get("real_order_submission") is not False or portfolio.get("real_capital_at_risk") is not False: reasons.append("portfolio_guard_contract_invalid")
    evidence = snapshot.get("evidence_allocator") or {}
    if evidence and (evidence.get("schema") != "polymarket_v7_evidence_capital_allocator_v2" or evidence.get("paper_only") is not True or evidence.get("authenticated_execution") is not False or evidence.get("real_order_submission") is not False or evidence.get("automatic_transfer") is not False): reasons.append("evidence_capital_allocator_missing_or_unsafe")
    fees = snapshot.get("fee_reward_registry") or {}
    if fees.get("schema") != "polymarket_v7_fee_reward_registry_v1" or fees.get("model_sha") != snapshot.get("sha") or fees.get("paper_only") is not True or fees.get("authenticated_execution") is not False or fees.get("real_order_submission") is not False or fees.get("unknown_fee_policy") != "NON_EXECUTABLE" or fees.get("unknown_reward_policy") != "ZERO_EXPECTED_VALUE": reasons.append("fee_reward_registry_missing_or_unsafe")
    age = _integer(snapshot.get("timestamp")) - _integer(fast.get("timestamp"))
    if fast.get("schema") != "polymarket_v7_structural_arb_engine_status_v1" or fast.get("model_sha") != snapshot.get("sha") or fast.get("state") != "RUNNING" or fast.get("paper_only") is not True or fast.get("authenticated_execution") is not False or fast.get("real_order_submission") is not False or fast.get("real_capital_at_risk") is not False or fast.get("execution_authority") != "OPPORTUNITY_PROPOSAL_ONLY" or any(fast.get(k) is not False for k in ("capital_authority", "oms_authority", "inventory_authority", "ledger_writer_authority")) or not -5 <= age <= max_runtime_age: reasons.append("structural_arb_engine_missing_stale_or_unsafe")
    if maker.get("schema") != "polymarket_v7_professional_maker_status_v1" or maker.get("model_sha") != snapshot.get("sha") or maker.get("paper_only") is not True or maker.get("authenticated_execution") is not False or maker.get("real_order_submission") not in (None, False) or maker.get("killed") is True or maker.get("source") in (None, "", "not_started") or not _fresh_ms(maker, snapshot, max_runtime_age): reasons.append("professional_maker_missing_stale_or_unsafe")
    if selector.get("schema") != "polymarket_v7_maker_selector_status_v1" or selector.get("model_sha") != snapshot.get("sha") or selector.get("ready") is not True or selector.get("state") not in _MAKER_SELECTOR_OPERATIONAL_STATES or selector.get("paper_only") is not True or selector.get("authenticated_execution") is not False or selector.get("real_order_submission") is not False or not _fresh_ms(selector, snapshot, max_runtime_age): reasons.append("maker_selector_missing_stale_or_unsafe")
    if rotation.get("schema") != "polymarket_v7_maker_cohort_rotation_status_v1" or rotation.get("model_sha") != snapshot.get("sha") or rotation.get("state") not in _MAKER_ROTATION_OPERATIONAL_STATES or rotation.get("paper_only") is not True or rotation.get("authenticated_execution") is not False or rotation.get("real_order_submission") is not False or not _fresh_ms(rotation, snapshot, max_runtime_age): reasons.append("maker_cohort_supervisor_missing_stale_or_unsafe")
    if universe.get("schema") != "polymarket_v7_adaptive_universe_status_v1" or universe.get("model_sha") != snapshot.get("sha") or universe.get("state") != "OPERATIONAL" or universe.get("discovery_exhaustive") is not True or universe.get("pagination_loop_guard_hit") is not False or universe.get("paper_only") is not True or universe.get("authenticated_execution") is not False or universe.get("real_order_submission") is not False or _integer(universe.get("eligible_markets")) <= 0 or not _fresh_ms(universe, snapshot, max_runtime_age): reasons.append("adaptive_universe_missing_stale_or_unsafe")
    if snapshot.get("runtime_alive") is not True: reasons.append("execution_not_alive")
    if canonical.get("paper_only") is not True or canonical.get("authenticated_execution") is not False: reasons.append("canonical_economics_missing_or_unsafe")
    if canonical.get("expected_model_sha") != snapshot.get("sha"): reasons.append("canonical_economics_sha_mismatch")
    if not ledger.get("present"): reasons.append("canonical_ledger_missing")
    elif not ledger.get("valid"): reasons.append("canonical_ledger_invalid_or_mixed_sha")
    rows = _integer((snapshot.get("trade_tape") or {}).get("rows"))
    if rows <= 0 and not _verified_no_flow(snapshot.get("trade_recorder") or {}, max_runtime_age): reasons.append("trade_tape_empty_or_unverified_no_standard_clob_flow")
    if _number(ages.get("runtime"), math.inf) > max_runtime_age: reasons.append("runtime_stale")
    if _number(ages.get("economics"), math.inf) > max_runtime_age: reasons.append("economics_stale")
    if rows > 0 and _number(ages.get("trade_tape"), math.inf) > max_runtime_age: reasons.append("trade_tape_stale")
    if _number(ages.get("portfolio"), math.inf) > max_supervisor_age: reasons.append("portfolio_guard_stale")
    if (snapshot.get("economics") or {}).get("killed"): reasons.append("runtime_killed")
    retention, operations = (snapshot.get("operations") or {}).get("retention") or {}, snapshot.get("operations") or {}
    if retention.get("schema") != "polymarket_v7_retention_status_v1" or retention.get("paper_only") is not True or retention.get("authenticated_execution") is not False or retention.get("expected_sha") != snapshot.get("sha") or _number(operations.get("retention_age"), math.inf) > 7200: reasons.append("retention_service_missing_or_stale")
    limit = _number((snapshot.get("authority") or {}).get("max_drawdown"))
    if limit > 0 and _number((snapshot.get("economics") or {}).get("drawdown")) >= limit - 1e-12: reasons.append("drawdown_limit_breached")
    external = snapshot.get("external_fair") or {}
    if external.get("external_fair_required_markets", 0) and not external.get("shadow_zero_authority", True): reasons.extend(map(str, external.get("hard_reasons", [])))
    return sorted(set(reasons))


def _append_maker_metrics(lines: list[str], snapshot: dict[str, Any]) -> None:
    lab, diagnostics = snapshot.get("maker_lab") or {}, snapshot.get("maker_diagnostics") or {}
    quality = lab.get("quality") if isinstance(lab.get("quality"), dict) else {}
    lines.extend([_metric("polymarket_maker_lab_present", bool(lab.get("present"))), _metric("polymarket_maker_lab_orders", lab.get("orders")), _metric("polymarket_maker_lab_lifetime_arm_known_orders", quality.get("lifetime_arm_known_orders"))])
    for row in lab.get("segments") if isinstance(lab.get("segments"), list) else []:
        lines.append(_metric("polymarket_maker_lab_segment_orders", row.get("orders"), {k: row.get(k, "UNKNOWN") for k in ("action", "variant", "dimension", "bucket")}))
    for row in lab.get("conditionals") if isinstance(lab.get("conditionals"), list) else []:
        lines.append(_metric("polymarket_maker_lab_conditional_orders", row.get("orders"), {k: row.get(k, "UNKNOWN") for k in ("action", "toxicity", "queue")}))
    for row in lab.get("markets") if isinstance(lab.get("markets"), list) else []:
        lines.append(_metric("polymarket_maker_lab_market_realized_pnl_usd", row.get("realized_pnl"), {"market": row.get("market", "UNKNOWN"), "action": row.get("action", "UNKNOWN")}))
    for reason, count in sorted((diagnostics.get("reason_counts") or {}).items()): lines.append(_metric("polymarket_v7_maker_decision_reason_total", count, {"reason": reason}))


def render_prometheus(snapshot: dict[str, Any]) -> str:
    runtime, economics = snapshot.get("runtime") or {}, snapshot.get("economics") or {}
    canonical, ledger = snapshot.get("canonical_economics") or {}, snapshot.get("ledger") or {}
    total, operations = ledger.get("total") or {}, snapshot.get("operations") or {}
    selector, rotation, diagnostics = snapshot.get("maker_selector") or {}, snapshot.get("maker_rotation") or {}, snapshot.get("maker_diagnostics") or {}
    universe, reasons = snapshot.get("universe") or {}, health_reasons(snapshot)
    scope_ok = _scope_valid(snapshot)
    lines = [
        _metric("polymarket_v7_health", not reasons), _metric("polymarket_v7_runtime_info", 1),
        _metric("polymarket_v7_runtime_identity_info", 1, {"sha": snapshot.get("sha", "unknown"), "run_id": runtime.get("run_id", "")}),
        _metric("polymarket_runtime_info", 1, {"adapter": "v7_native", "run_root": snapshot.get("run_root", "unknown"), "version": "v7"}),
        _metric("polymarket_v7_operator_authority_valid", (snapshot.get("authority") or {}).get("valid")), _metric("polymarket_v7_authority_max_drawdown_ratio", (snapshot.get("authority") or {}).get("max_drawdown")),
        _metric("polymarket_v7_paper_only_contract_ok", runtime.get("paper_only") is True and runtime.get("real_order_submission") is False), _metric("polymarket_v7_authenticated_execution_disabled", runtime.get("authenticated_execution") is False),
        _metric("polymarket_v7_exact_sha_ok", runtime.get("model_sha") == snapshot.get("sha")), _metric("polymarket_v7_execution_alive", snapshot.get("runtime_alive")), _metric("polymarket_v7_supervisor_alive", operations.get("supervisor_alive")), _metric("polymarket_v7_single_writer_ok", operations.get("single_writer")), _metric("polymarket_v7_ledger_writable", operations.get("ledger_writable")),
        _metric("polymarket_v7_runtime_uptime_seconds", operations.get("runtime_uptime")), _metric("polymarket_v7_restart_count_window", operations.get("restart_count")), _metric("polymarket_v7_disk_free_ratio", operations.get("disk_free_ratio")),
        _metric("polymarket_v7_live_algorithm_count", 2), _metric("polymarket_v7_legacy_algorithm_count", 0), _metric("polymarket_v7_live_algorithm_scope_wired", scope_ok), _metric("polymarket_v7_live_model_scope_wired", scope_ok), _metric("polymarket_v7_economic_new_risk_ready", runtime.get("economic_new_risk_ready")),
        _metric("polymarket_runtime_equity_usd", economics.get("equity")), _metric("polymarket_runtime_pnl_usd", economics.get("pnl")), _metric("polymarket_runtime_realized_pnl_usd", economics.get("realized_pnl")), _metric("polymarket_runtime_drawdown_ratio", economics.get("drawdown")), _metric("polymarket_runtime_killed", economics.get("killed")),
        _metric("polymarket_v7_canonical_submitted_units", canonical.get("submitted_units")), _metric("polymarket_v7_canonical_complete_units", canonical.get("complete_units")), _metric("polymarket_v7_ledger_valid", ledger.get("valid")), _metric("polymarket_v7_portfolio_reconciled", (snapshot.get("reconciliation") or {}).get("reconciled")), _metric("polymarket_v7_reconciliation_divergences", len((snapshot.get("reconciliation") or {}).get("reason_codes") or [])),
        _metric("polymarket_v7_trade_tape_rows", (snapshot.get("trade_tape") or {}).get("rows")), _metric("polymarket_v7_trade_tape_assets", (snapshot.get("trade_tape") or {}).get("assets")), _metric("polymarket_v7_trade_tape_no_standard_clob_flow", _verified_no_flow(snapshot.get("trade_recorder") or {}, 180)), _metric("polymarket_v7_latency_samples_present", (snapshot.get("maker_latency") or {}).get("present")),
        _metric("polymarket_v7_component_ready", "professional_maker_missing_stale_or_unsafe" not in reasons, {"component": "professional_maker"}), _metric("polymarket_v7_component_ready", "structural_arb_engine_missing_stale_or_unsafe" not in reasons, {"component": "fast_structural"}),
        _metric("polymarket_v7_maker_selector_ready", selector.get("ready") and selector.get("state") in _MAKER_SELECTOR_OPERATIONAL_STATES), _metric("polymarket_v7_maker_selector_fallback_active", selector.get("degraded")), _metric("polymarket_v7_maker_runtime_selection_pinned", selector.get("runtime_selection_pinned")), _metric("polymarket_v7_maker_candidate_rotation_pending", selector.get("candidate_rotation_pending")), _metric("polymarket_v7_maker_candidate_selected_markets", selector.get("candidate_selected_count")),
        _metric("polymarket_v7_maker_candidate_fresh_flow_eligible", selector.get("candidate_fresh_flow_eligible")), _metric("polymarket_v7_maker_candidate_sell_flow_30s_markets", selector.get("candidate_selected_with_sell_flow_30s")), _metric("polymarket_v7_maker_candidate_sell_flow_2m_markets", selector.get("candidate_selected_with_sell_flow_2m")), _metric("polymarket_v7_maker_candidate_max_last_sell_age_seconds", selector.get("candidate_max_last_sell_age_seconds")),
        _metric("polymarket_v7_maker_cohort_supervisor_ready", rotation.get("state") in _MAKER_ROTATION_OPERATIONAL_STATES), _metric("polymarket_v7_maker_cohort_rotations_total", rotation.get("rotation_count")), _metric("polymarket_v7_maker_rotation_candidate_confirmations", rotation.get("candidate_confirmations")), _metric("polymarket_v7_maker_rotation_required_confirmations", rotation.get("candidate_required_confirmations")), _metric("polymarket_v7_maker_rotation_cooldown_remaining_seconds", rotation.get("rotation_cooldown_remaining_seconds")), _metric("polymarket_v7_maker_paused_no_fresh_flow", rotation.get("fresh_flow_pause_active")),
        _metric("polymarket_v7_maker_feed_connected_workers", diagnostics.get("feed_connected_workers")), _metric("polymarket_v7_maker_feed_messages_total", diagnostics.get("feed_messages")), _metric("polymarket_v7_maker_decisions_total", diagnostics.get("decisions")), _metric("polymarket_v7_maker_quote_intents_total", diagnostics.get("quote_intents")), _metric("polymarket_v7_maker_rejected_positive_point_ev_total", diagnostics.get("rejected_positive_point_ev")), _metric("polymarket_v7_maker_best_rejected_point_ev_per_share", diagnostics.get("best_rejected_point_ev_per_share")),
        _metric("polymarket_v7_universe_discovered_markets", universe.get("discovered_markets")), _metric("polymarket_v7_universe_eligible_markets", universe.get("eligible_markets")), _metric("polymarket_v7_universe_skipped_markets", universe.get("skipped_markets")), _metric("polymarket_v7_universe_pages", universe.get("pages")), _metric("polymarket_v7_universe_scan_duration_milliseconds", universe.get("scan_duration_ms")), _metric("polymarket_v7_universe_discovery_exhaustive", universe.get("discovery_exhaustive")),
    ]
    configured = set(runtime.get("economic_engines") or [])
    for engine in LIVE_ALGORITHMS: lines.append(_metric("polymarket_v7_economic_engine_configured", engine in configured, {"engine": engine}))
    for name, row in sorted((snapshot.get("algorithms") or {}).items()):
        for field, metric in (("equity", "equity_usd"), ("budget", "budget_usd"), ("killed", "killed")): lines.append(_metric(f"polymarket_v7_live_algorithm_{metric}", row.get(field), {"algorithm": name}))
    coordinator = snapshot.get("global_coordinator") or {}
    for field in ("crypto_gross_exposure_usd", "crypto_net_directional_exposure_usd", "crypto_cluster_exposure_usd"): lines.append(_metric("polymarket_v7_" + field, coordinator.get(field)))
    models = {(r.get("asset"), r.get("horizon")): r for r in (snapshot.get("crypto_model_registry") or {}).get("models", []) if isinstance(r, dict)}
    for row in (snapshot.get("crypto_registry") or {}).get("contexts", []):
        labels = {"asset": row.get("asset"), "horizon": row.get("horizon"), "contract_family": row.get("contract_family"), "authority": row.get("authority")}; model = models.get((row.get("asset"), row.get("horizon")), {})
        lines.extend([_metric("polymarket_v7_crypto_context_registered", row.get("enabled"), labels), _metric("polymarket_v7_crypto_context_zero_authority", row.get("research_only"), labels), _metric("polymarket_v7_crypto_context_new_risk_authorized", model.get("new_risk_authorized"), labels), _metric("polymarket_v7_crypto_context_model_registered", bool(model.get("artifact")), labels)])
    for state, age in sorted((snapshot.get("ages") or {}).items()): lines.append(_metric("polymarket_v7_state_age_seconds", age, {"state": state}))
    for tier, count in sorted((universe.get("tier_counts") or {}).items()): lines.append(_metric("polymarket_v7_universe_tier_markets", count, {"tier": tier}))
    capacities = universe.get("resource_capacities") if isinstance(universe.get("resource_capacities"), dict) else {}
    for tier, values in (("HOT", capacities.get("hot_limits")), ("WARM", capacities.get("warm_limits"))):
        for dimension, value in sorted((values or {}).items()):
            lines.append(_metric("polymarket_v7_universe_resource_limit", value, {"tier": tier, "dimension": dimension}))
    for reason in reasons: lines.append(_metric("polymarket_v7_health_reason", 1, {"reason": reason}))
    for reason, count in sorted((ledger.get("invalid_reason_counts") or {}).items()): lines.append(_metric("polymarket_v7_ledger_invalid_reason_rows", count, {"reason": reason}))
    for key, metric in {"opportunities":"opportunities", "candidates":"candidates", "makes":"makes", "takes":"takes", "arbs":"arbs", "cancels":"cancels", "withdraws":"withdraws", "orders_submitted":"orders_submitted", "effective_orders":"effective_orders", "fills":"fills", "complete_fills":"complete_fills", "partial_fills":"partial_fills", "unwinds":"unwinds"}.items(): lines.append(_metric("polymarket_execution_" + metric, total.get(key)))
    for strategy, row in sorted((ledger.get("strategies") or {}).items()):
        lines.append(_metric("polymarket_strategy_ledger_orders_submitted", row.get("orders_submitted"), {"strategy": strategy}))
        lines.append(_metric("polymarket_strategy_ledger_fills", row.get("fills"), {"strategy": strategy}))
    lines.extend([_metric("polymarket_execution_final_pnl_usd", total.get("final_pnl")), _metric("polymarket_execution_capital_hours", _number(total.get("capital_duration_ms"))/3_600_000)])
    for horizon, value in sorted((total.get("markout_sum") or {}).items()):
        count = _number((total.get("markout_count") or {}).get(horizon)); lines.append(_metric("polymarket_execution_mean_markout", _number(value)/count if count else 0, {"horizon": horizon}))
    for stage, row in sorted(((snapshot.get("maker_latency") or {}).get("stages") or {}).items()):
        for percentile in ("p50", "p90", "p95", "p99", "p99_9", "max"): lines.append(_metric("polymarket_v7_latency_stage_nanoseconds", row.get(percentile), {"stage": stage, "percentile": percentile}))
    _append_maker_metrics(lines, snapshot)
    from exporter_v7_fillability import _append_fillability_metrics
    from exporter_v7_external import _append_external_fair_metrics
    _append_fillability_metrics(lines, snapshot.get("maker_fillability") or {})
    _append_external_fair_metrics(lines, snapshot.get("external_fair") or {})
    return "\n".join(lines) + "\n"


class SnapshotCache:
    def __init__(self, run_root: Path, repository_root: Path, *, refresh_seconds: float = 10.0) -> None:
        self.run_root, self.repository_root, self.refresh_seconds = Path(run_root), Path(repository_root), max(1.0, float(refresh_seconds)); self._lock = threading.Lock(); self._ready = threading.Event(); self._stop = threading.Event(); self._thread = None; self._snapshot = None; self._metrics = b""; self._maker_fillability = b"{}\n"; self._external_fair = b"{}\n"; self._completed_monotonic = 0.0; self._completed_wall = 0.0; self._refresh_duration = 0.0; self._refresh_errors = 0; self._last_error = ""
    def start(self) -> None:
        if self._thread is None: self._thread = threading.Thread(target=self._refresh_loop, daemon=True); self._thread.start()
    def stop(self) -> None:
        self._stop.set()
        if self._thread: self._thread.join(timeout=max(2.0, self.refresh_seconds + 1))
    def wait_ready(self, timeout: float | None = None) -> bool: return self._ready.wait(timeout)
    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                snapshot = collect_snapshot(self.run_root, self.repository_root); duration = time.monotonic()-started; wall = time.time(); metrics = render_prometheus(snapshot).rstrip()+f"\npolymarket_v7_exporter_snapshot_generated_unixtime {wall}\npolymarket_v7_exporter_snapshot_refresh_duration_seconds {duration}\npolymarket_v7_exporter_snapshot_refresh_errors_total {self._refresh_errors}\n"
                with self._lock: self._snapshot=snapshot; self._metrics=metrics.encode(); self._maker_fillability=(json.dumps(snapshot.get("maker_fillability") or {},sort_keys=True)+"\n").encode(); self._external_fair=(json.dumps(snapshot.get("external_fair") or {},sort_keys=True)+"\n").encode(); self._completed_monotonic=time.monotonic(); self._completed_wall=wall; self._refresh_duration=duration; self._last_error=""
                self._ready.set()
            except Exception as exc:
                with self._lock: self._refresh_errors += 1; self._last_error=f"{type(exc).__name__}:{exc}"
            self._stop.wait(max(.1, self.refresh_seconds-(time.monotonic()-started)))
    def read(self) -> dict[str, Any]:
        with self._lock: return {"ready":self._snapshot is not None,"snapshot":self._snapshot,"metrics":self._metrics,"maker_fillability":self._maker_fillability,"external_fair":self._external_fair,"age_seconds":max(0,time.monotonic()-self._completed_monotonic) if self._completed_monotonic else math.inf,"completed_wall":self._completed_wall,"refresh_duration_seconds":self._refresh_duration,"refresh_errors":self._refresh_errors,"last_error":self._last_error}


class ExporterHandler(BaseHTTPRequestHandler):
    run_root=Path("runs/paper_v7_live"); repository_root=Path("."); max_runtime_age=180; max_supervisor_age=30; max_snapshot_age=45.0; snapshot_cache: SnapshotCache | None=None
    def log_message(self,_format:str,*_args:object)->None: return
    def do_GET(self)->None:
        cached=self.snapshot_cache.read() if self.snapshot_cache else None
        if cached is None:
            snapshot=collect_snapshot(self.run_root,self.repository_root); cached={"ready":True,"snapshot":snapshot,"metrics":render_prometheus(snapshot).encode(),"maker_fillability":(json.dumps(snapshot.get("maker_fillability") or {})+"\n").encode(),"external_fair":(json.dumps(snapshot.get("external_fair") or {})+"\n").encode(),"age_seconds":0}
        if not cached.get("ready"): payload=b'{"ok":false,"reasons":["exporter_snapshot_not_ready"]}\n'; self.send_response(503); content="application/json"
        elif self.path=="/metrics": payload=cached["metrics"]; self.send_response(200); content="text/plain; version=0.0.4"
        elif self.path=="/healthz":
            reasons=health_reasons(cached["snapshot"],max_runtime_age=self.max_runtime_age,max_supervisor_age=self.max_supervisor_age)
            if _number(cached.get("age_seconds"),math.inf)>self.max_snapshot_age: reasons.append("exporter_snapshot_stale")
            reasons=sorted(set(reasons)); payload=(json.dumps({"ok":not reasons,"reasons":reasons},sort_keys=True)+"\n").encode(); self.send_response(200 if not reasons else 503); content="application/json"
        elif self.path=="/maker-fillability.json": payload=cached["maker_fillability"]; self.send_response(200); content="application/json"
        elif self.path=="/external-fair.json": payload=cached["external_fair"]; self.send_response(200); content="application/json"
        else: payload=b"not found\n"; self.send_response(404); content="text/plain"
        self.send_header("Content-Type",content+"; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload)


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--run-root",type=Path,default=Path("runs/paper_v7_live")); parser.add_argument("--repository-root",type=Path,default=Path(".")); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=9108); parser.add_argument("--max-runtime-age",type=int,default=180); parser.add_argument("--max-supervisor-age",type=int,default=30); parser.add_argument("--snapshot-refresh-seconds",type=float,default=10); parser.add_argument("--max-snapshot-age",type=float,default=45); args=parser.parse_args()
    ExporterHandler.run_root=args.run_root; ExporterHandler.repository_root=args.repository_root; ExporterHandler.max_runtime_age=args.max_runtime_age; ExporterHandler.max_supervisor_age=args.max_supervisor_age; ExporterHandler.max_snapshot_age=args.max_snapshot_age
    cache=SnapshotCache(args.run_root,args.repository_root,refresh_seconds=args.snapshot_refresh_seconds); cache.start(); ExporterHandler.snapshot_cache=cache; server=ThreadingHTTPServer((args.host,args.port),ExporterHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); cache.stop()
    return 0


if __name__=="__main__": raise SystemExit(main())
