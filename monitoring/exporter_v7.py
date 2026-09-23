#!/usr/bin/env python3
"""Prometheus exporter for the canonical crypto-only V7 PAPER runtime."""
from __future__ import annotations

import argparse, csv, hashlib, io, json, math, os, shutil, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from v7_operator_truth import append_operator_metrics, prometheus_number
from v7_external_fair import summarize_external_fair
from v7_ledger_metrics import summarize_ledger
from v7_maker_fillability_exact import summarize_best_available_fillability
from v7_maker_microstructure import summarize_maker_microstructure
from v7_portfolio_reconciliation import reconcile as reconcile_portfolio
from v7_multi_crypto_performance import (
    render_prometheus as render_multi_crypto_prometheus,
    render_shadow_prometheus,
    summarize_multi_crypto,
    summarize_shadow_runtime,
)
from v7_runtime_contract import (
    MAKER_ROTATION_OPERATIONAL_STATES as _MAKER_ROTATION_OPERATIONAL_STATES,
    MAKER_SELECTOR_OPERATIONAL_STATES as _MAKER_SELECTOR_OPERATIONAL_STATES,
)

LIVE_ALGORITHMS = ("CRYPTO_SETTLEMENT_ENGINE",)
_FILLABILITY_CACHE_KEY: tuple[str, str, int] | None = None
_FILLABILITY_CACHE_VALUE: dict[str, Any] | None = None


def _number(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _lead_lag_state(run_root: Path, runtime_sha: str) -> dict[str, Any]:
    path = run_root / "research/lead_lag_taker_v1/status.json"
    status = _json(path)
    pnl = _optional_number(status.get("realized_pnl"))
    valid = bool(
        status
        and status.get("schema") == "polymarket_v7_lead_lag_taker_v1_status"
        and status.get("model_sha") == runtime_sha
        and status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and pnl is not None
    )
    return {
        "present": bool(status), "valid": valid,
        "realized_pnl": pnl if valid else None,
        "entries": max(0, _integer(status.get("entries"), 0)),
        "settled": max(0, _integer(status.get("settled"), 0)),
        "open_positions": max(0, _integer(status.get("open_positions"), 0)),
        "status_path": str(path),
    }


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
    env = os.environ.get("PM_V7_MODEL_SHA", "").strip()
    if len(env) == 40 and all(ch in "0123456789abcdef" for ch in env):
        return env
    try:
        value = (root / "deploy/london/runtime_sha").read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if len(value) == 40 and all(ch in "0123456789abcdef" for ch in value):
        return value
    # Development/test checkouts retain .git; the London bundle intentionally does not.
    try:
        value = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return value if len(value) == 40 and all(ch in "0123456789abcdef" for ch in value) else "unknown"


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
    return f"{name}{suffix} {prometheus_number(value)}"


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
        and status.get("flow_regime") == "CRYPTO_CLOB_NO_MATCHING_TRADES"
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


def _tail_csv(path: Path, maximum_bytes: int = 4 * 1024 * 1024) -> list[dict[str, str]]:
    """Read a bounded suffix while retaining the original CSV header."""
    try:
        with path.open("rb") as handle:
            header = handle.readline()
            header_end = handle.tell()
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(header_end, size - maximum_bytes)
            handle.seek(start)
            if start > header_end:
                handle.readline()  # discard the partial first row
            body = handle.read()
        text = (header + body).decode("utf-8", errors="replace")
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    except (OSError, csv.Error):
        return []


def _runtime_latency(run_root: Path) -> dict[str, Any]:
    maker = _maker_latency(run_root / "micro_maker/latency.csv")
    return {
        "present": bool(maker.get("present")),
        "rows": int(maker.get("rows") or 0),
        "stages": maker.get("stages") or {},
        "sources": {"professional_maker": bool(maker.get("present"))},
    }

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


def _operations(run_root: Path, runtime: dict[str, Any], now: int | float | None) -> dict[str, Any]:
    supervisor = _json(run_root / "control/supervisor_status.json")
    retention = _json(run_root / "control/london_buffer_retention_status.json")
    try:
        lock_pid = int((run_root / "control/runtime.lock/pid").read_text().strip())
    except (OSError, ValueError):
        lock_pid = 0
    writer = _json(run_root / "control/ledger_writer_status.json")
    # The writer is read after potentially expensive ledger/analytics scans.
    # Compare with its own observation clock, not the snapshot-start timestamp:
    # otherwise a healthy concurrent update falsely appears to be in the future.
    # Explicit clocks are preserved for deterministic tests and historical reads.
    now = time.time() if now is None else now
    writer_valid = (
        writer.get("schema") == "polymarket_v7_ledger_writer_status_v1"
        and writer.get("model_sha") == runtime.get("model_sha")
        and bool(runtime.get("run_id")) and writer.get("run_id") == runtime.get("run_id")
        and writer.get("paper_only") is True
        and writer.get("authenticated_execution") is False
        and writer.get("real_order_submission") is False
        and writer.get("healthy") is True and _pid_alive(writer.get("pid"))
        and -1 <= now - _number(writer.get("timestamp_ms")) / 1000 <= 5
    )
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
        "ledger_writable": writer_valid and writer.get("ledger_writable") is True,
        "ledger_writer_status": writer,
        "writer_observed_at_unix": now,
        "exporter_has_write_permission": os.access(run_root / "ledger", os.W_OK),
        "disk_free_ratio": free_ratio, "retention_age": _age(now, retention.get("timestamp")),
    }



def _prom_label(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_pure_arb_metrics(status: dict[str, Any]) -> list[str]:
    safe = (
        status.get("schema") == "polymarket_v7_pure_arb_paper_status_v1"
        and status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and status.get("real_capital_at_risk") is False
    )
    lines = [
        _metric("polymarket_pure_arb_up", safe and status.get("state") == "running"),
        _metric("polymarket_pure_arb_cycles_total", status.get("cycles_total")),
        _metric("polymarket_pure_arb_paper_locked_pnl_pre_gas_usd_total",
                status.get("paper_locked_pnl_pre_gas_total")),
        _metric("polymarket_pure_arb_conservative_locked_pnl_after_reserve_usd_total",
                status.get("conservative_locked_pnl_after_reserve_total")),
        _metric("polymarket_pure_arb_reserve_per_share", status.get("reserve_per_share")),
        _metric("polymarket_pure_arb_maximum_leg_skew_ms", status.get("maximum_leg_skew_ms")),
        _metric("polymarket_pure_arb_maximum_receive_to_decision_ns",
                status.get("maximum_receive_to_decision_ns")),
        _metric("polymarket_pure_arb_evaluations_total", status.get("evaluations")),
        _metric("polymarket_pure_arb_fee_blocked_evaluations_total",
                status.get("fee_blocked_evaluations")),
        _metric("polymarket_pure_arb_fee_ready_contexts", status.get("fee_ready_contexts")),
        _metric("polymarket_pure_arb_active_contexts", status.get("active_contexts")),
        _metric("polymarket_pure_arb_subscribed_contexts", status.get("subscribed_contexts")),
        _metric("polymarket_pure_arb_preloaded_contexts", status.get("preloaded_contexts")),
        _metric("polymarket_pure_arb_subscribed_fee_ready_contexts",
                status.get("subscribed_fee_ready_contexts")),
        _metric("polymarket_pure_arb_last_receive_to_decode_ns",
                status.get("last_receive_to_decode_ns")),
        _metric("polymarket_pure_arb_last_decode_to_enqueue_ns",
                status.get("last_decode_to_enqueue_ns")),
        _metric("polymarket_pure_arb_last_receive_to_enqueue_ns",
                status.get("last_receive_to_enqueue_ns")),
        _metric("polymarket_pure_arb_last_queue_wait_ns",
                status.get("last_queue_wait_ns")),
        _metric("polymarket_pure_arb_last_decision_compute_ns",
                status.get("last_decision_compute_ns")),
        _metric("polymarket_pure_arb_max_decision_compute_ns",
                status.get("max_decision_compute_ns")),
        _metric("polymarket_pure_arb_last_receive_to_decision_ns",
                status.get("last_receive_to_decision_ns")),
        _metric("polymarket_pure_arb_max_receive_to_decision_ns",
                status.get("max_receive_to_decision_ns")),
    ]
    if not safe:
        return lines
    funnel = status.get("funnel") if isinstance(status.get("funnel"), dict) else {}
    for stage, value in sorted(funnel.items()):
        lines.append(_metric("polymarket_pure_arb_funnel_total", value, {"stage": stage}))
    for kind, field in (
        ("receive_to_decode", "receive_to_decode_ns"),
        ("decode_to_enqueue", "decode_to_enqueue_ns"),
        ("receive_to_enqueue", "receive_to_enqueue_ns"),
        ("queue_wait", "queue_wait_ns"),
        ("receive_to_decision", "receive_to_decision_ns"),
        ("decision_compute", "decision_compute_ns"),
    ):
        values = status.get(field) if isinstance(status.get(field), dict) else {}
        for percentile in ("p50", "p90", "p99", "p99_9", "max"):
            lines.append(_metric(
                "polymarket_pure_arb_latency_ns", values.get(percentile),
                {"kind": kind, "percentile": percentile}))
    lines.append(_metric("polymarket_pure_arb_latency_window_samples",
                         status.get("latency_window_samples")))
    for row in status.get("contexts") or []:
        if not isinstance(row, dict):
            continue
        asset = _prom_label(row.get("asset"))
        horizon = _prom_label(row.get("horizon"))
        context_labels = {"asset": asset, "horizon": horizon}
        lines.append(_metric("polymarket_pure_arb_context_window_active",
                             row.get("active_window") is True, context_labels))
        for field, kind in (
            ("buy_complete_set", "BUY_COMPLETE_SET"),
            ("sell_complete_set", "SELL_COMPLETE_SET"),
        ):
            value = row.get(field)
            if not isinstance(value, dict):
                continue
            labels = {"asset": asset, "horizon": horizon, "kind": kind}
            lines.extend([
                _metric("polymarket_pure_arb_context_active",
                        value.get("active") is True, labels),
                _metric("polymarket_pure_arb_context_cycles_total",
                        value.get("cycles"), labels),
                _metric("polymarket_pure_arb_context_paper_locked_pnl_pre_gas_usd_total",
                        value.get("paper_locked_pnl_pre_gas"), labels),
                _metric("polymarket_pure_arb_context_conservative_locked_pnl_after_reserve_usd_total",
                        value.get("conservative_locked_pnl_after_reserve"), labels),
                _metric("polymarket_pure_arb_context_last_edge_per_share",
                        value.get("last_edge_per_share"), labels),
                _metric("polymarket_pure_arb_context_max_edge_per_share",
                        value.get("max_edge_per_share"), labels),
                _metric("polymarket_pure_arb_context_last_executable_shares_l1",
                        value.get("last_executable_shares_l1"), labels),
                _metric("polymarket_pure_arb_context_last_executable_shares_l10",
                        value.get("last_executable_shares_l10"), labels),
                _metric("polymarket_pure_arb_context_last_locked_pnl_pre_gas_usd",
                        value.get("last_locked_pnl_pre_gas"), labels),
                _metric("polymarket_pure_arb_context_last_conservative_locked_pnl_after_reserve_usd",
                        value.get("last_conservative_locked_pnl_after_reserve"), labels),
            ])
    return lines



def _render_settlement_source_arb_metrics(status: dict[str, Any]) -> list[str]:
    safe = (
        status.get("schema") == "polymarket_v7_settlement_source_arb_status_v2"
        and status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and status.get("real_capital_at_risk") is False
    )
    lines = [
        _metric("polymarket_settlement_source_arb_up", safe and status.get("state") == "COLLECTING"),
        _metric("polymarket_settlement_source_arb_cycles_total", status.get("cycles")),
        _metric("polymarket_settlement_source_arb_locked_total", status.get("paper_locked_arbitrages")),
        _metric("polymarket_settlement_source_arb_locked_pnl_usd_total", status.get("locked_pnl")),
        _metric("polymarket_settlement_source_arb_pending_outcomes", status.get("pending_outcomes")),
        _metric("polymarket_settlement_source_arb_cached_markets", status.get("cached_markets")),
    ]
    if not safe:
        return lines
    for kind, row in sorted((status.get("by_kind") or {}).items()):
        if not isinstance(row, dict):
            continue
        labels = {"kind": _prom_label(kind)}
        lines.extend([
            _metric("polymarket_settlement_source_arb_kind_cycles_total", row.get("cycles"), labels),
            _metric("polymarket_settlement_source_arb_kind_locked_total",
                    row.get("paper_locked_arbitrages"), labels),
            _metric("polymarket_settlement_source_arb_kind_locked_pnl_usd_total",
                    row.get("locked_pnl"), labels),
        ])
    for state, count in sorted((status.get("states") or {}).items()):
        lines.append(_metric("polymarket_settlement_source_arb_state_total", count,
                             {"state": _prom_label(state)}))
    return lines


def _render_complete_set_maker_metrics(status: dict[str, Any]) -> list[str]:
    safe = (
        status.get("schema") == "polymarket_v7_two_sided_complete_set_shadow_status_v2"
        and status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and status.get("real_capital_at_risk") is False
    )
    lines = [
        _metric("polymarket_complete_set_maker_shadow_up",
                safe and status.get("state") == "COLLECTING"),
        _metric("polymarket_complete_set_maker_cycles_total", status.get("cycles")),
        _metric("polymarket_complete_set_maker_active_cycles", status.get("active_cycles")),
        _metric("polymarket_complete_set_maker_paired_fill_probability",
                status.get("paired_fill_probability_direct")),
        _metric("polymarket_complete_set_maker_both_any_probability",
                status.get("both_any_probability_direct")),
        _metric("polymarket_complete_set_maker_one_leg_probability",
                status.get("one_leg_probability_direct")),
        _metric("polymarket_complete_set_maker_total_shadow_pnl_usd",
                status.get("sum_total_shadow_pnl")),
        _metric("polymarket_complete_set_maker_mean_shadow_pnl_usd",
                status.get("mean_total_shadow_pnl")),
        _metric("polymarket_complete_set_maker_total_legging_loss_usd",
                status.get("total_legging_loss")),
        _metric("polymarket_complete_set_maker_mean_legging_loss_usd",
                status.get("mean_legging_loss")),
        _metric("polymarket_complete_set_maker_reserve_per_share",
                status.get("reserve_per_share")),
        _metric("polymarket_complete_set_maker_depth_fraction",
                status.get("depth_fraction")),
        _metric("polymarket_complete_set_maker_queue_ahead_multiplier",
                status.get("queue_ahead_multiplier")),
    ]
    if not safe:
        return lines
    for state, count in sorted((status.get("states") or {}).items()):
        lines.append(_metric("polymarket_complete_set_maker_state_total", count,
                             {"state": _prom_label(state)}))
    for stage, count in sorted((status.get("funnel") or {}).items()):
        lines.append(_metric("polymarket_complete_set_maker_funnel_total", count,
                             {"stage": _prom_label(stage)}))
    for reason, count in sorted((status.get("cancels") or {}).items()):
        lines.append(_metric("polymarket_complete_set_maker_cancel_total", count,
                             {"reason": _prom_label(reason)}))
    for context, row in sorted((status.get("by_context") or {}).items()):
        if not isinstance(row, dict):
            continue
        asset, _, horizon = str(context).partition(":")
        labels = {"asset": _prom_label(asset), "horizon": _prom_label(horizon)}
        for field, metric in (
            ("cycles", "cycles_total"),
            ("paired_full", "paired_full_total"),
            ("one_leg", "one_leg_total"),
            ("paired_fill_probability_direct", "paired_fill_probability"),
            ("mean_total_shadow_pnl", "mean_shadow_pnl_usd"),
        ):
            lines.append(_metric(
                "polymarket_complete_set_maker_context_" + metric,
                row.get(field), labels))
    return lines


def _render_cross_market_exact_arb_metrics(status: dict[str, Any]) -> list[str]:
    safe = (
        status.get("schema") == "polymarket_v7_cross_market_exact_arb_status_v1"
        and status.get("paper_only") is True
        and status.get("authenticated_execution") is False
        and status.get("real_order_submission") is False
        and status.get("real_capital_at_risk") is False
    )
    return [
        _metric("polymarket_cross_market_exact_arb_up",
                safe and status.get("state") == "COLLECTING"),
        _metric("polymarket_cross_market_exact_arb_identity_groups",
                status.get("identity_groups")),
        _metric("polymarket_cross_market_exact_arb_pairs_checked_total",
                status.get("pairs_checked")),
        _metric("polymarket_cross_market_exact_arb_opportunities",
                status.get("opportunities_count")),
        _metric("polymarket_cross_market_exact_arb_locked_pnl_capacity_usd",
                status.get("locked_pnl_capacity")),
    ]


def _render_unified_exact_arb_graph_metrics(status: dict[str, Any]) -> list[str]:
    safe = (status.get("schema") == "polymarket_v7_unified_exact_arb_graph_shadow_status_v1"
            and status.get("paper_only") is True and status.get("authenticated_execution") is False
            and status.get("real_order_submission") is False and status.get("real_capital_at_risk") is False
            and status.get("automatic_promotion") is False
            and status.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY")
    funnel = status.get("funnel") if isinstance(status.get("funnel"), dict) else {}
    lines = [_metric("exact_arb_graph_up", safe and status.get("state") == "COLLECTING"),
             _metric("exact_arb_graph_relations", status.get("relations_compiled")),
             _metric("exact_arb_relations_evaluated_total", status.get("relations_evaluated")),
             _metric("exact_arb_candidates_total", funnel.get("candidate_emitted", 0))]
    for key, value in sorted(funnel.items()):
        if key.startswith("survival_"):
            continue
        lines.append(_metric("exact_arb_graph_funnel_total", funnel.get(key, 0), {"stage": key}))
    for family, stages in sorted((status.get("funnel_by_family") or {}).items()):
        if not isinstance(stages, dict):
            continue
        for stage, value in sorted(stages.items()):
            lines.append(_metric("exact_arb_graph_family_funnel_total", value,
                                 {"family": family, "stage": stage}))
    for arm in (1, 5, 10, 25, 50):
        checked = funnel.get(f"survival_{arm}ms_checked", 0)
        survived = funnel.get(f"survival_{arm}ms", 0)
        lines.append(_metric("exact_arb_survival_total", survived, {"delay_ms": arm}))
        lines.append(_metric("exact_arb_survival_checked_total", checked, {"delay_ms": arm}))
    for reason, count in sorted((status.get("rejection_reasons") or {}).items()):
        lines.append(_metric("exact_arb_graph_rejections_total", count, {"reason": reason}))
    for family, reasons in sorted((status.get("rejection_reasons_by_family") or {}).items()):
        if not isinstance(reasons, dict):
            continue
        for reason, count in sorted(reasons.items()):
            lines.append(_metric("exact_arb_graph_family_rejections_total", count,
                                 {"family": family, "reason": reason}))
    for key, values in sorted((status.get("near_arbitrage") or {}).items()):
        if not isinstance(values, dict): continue
        family, _, stage = str(key).partition(":")
        for percentile, value in sorted(values.items()):
            lines.append(_metric("exact_arb_distance_to_arbitrage", value,
                                 {"family": family or "UNKNOWN", "stage": stage or "UNKNOWN",
                                  "percentile": percentile}))
    for percentile, value in sorted((status.get("evaluation_latency_us") or {}).items()):
        lines.append(_metric("exact_arb_graph_evaluation_latency_microseconds", value,
                             {"percentile": percentile}))
    timestamp = status.get("timestamp_ms")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        lines.append(_metric("exact_arb_graph_generation_age_seconds",
                             max(0.0, time.time() - float(timestamp) / 1000.0)))
    return lines


def _render_unified_exact_arb_graph_execution_metrics(status: dict[str, Any]) -> list[str]:
    safe = (status.get("schema") == "polymarket_v7_pure_arb_exchange_execution_status_v1"
            and status.get("paper_only") is True and status.get("authenticated_execution") is False
            and status.get("real_order_submission") is False and status.get("real_capital_at_risk") is False
            and status.get("execution_authority") == "ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW")
    lines = [_metric("exact_arb_graph_execution_shadow_up", safe and status.get("state") == "COLLECTING"),
             _metric("exact_arb_graph_counterfactual_scenarios_total", status.get("evaluated")),
             _metric("exact_arb_graph_counterfactual_only_total", status.get("counterfactual_only_scenarios"))]
    if not safe:
        return lines
    for state, count in sorted((status.get("states") or {}).items()):
        lines.append(_metric("exact_arb_graph_counterfactual_state_total", count, {"state": state}))
    for mode, summary in sorted((status.get("by_execution_mode") or {}).items()):
        if not isinstance(summary, dict):
            continue
        lines.append(_metric("exact_arb_graph_counterfactual_fills_total", summary.get("paired"), {"mode": mode}))
        lines.append(_metric("exact_arb_graph_counterfactual_one_leg_unwound_total", summary.get("one_leg_unwound"), {"mode": mode}))
        lines.append(_metric("exact_arb_graph_counterfactual_pnl_usd", summary.get("sum_pnl_after_reserve"), {"mode": mode}))
    return lines


def collect_snapshot(run_root: Path, repository_root: Path | None = None, *, now: int | None = None, multi_crypto_shadow_run_root: Path | None = None, include_profit_experiment_report: bool = True) -> dict[str, Any]:
    live_observation_clock = now is None
    now = int(time.time()) if now is None else int(now)
    run_root = run_root.resolve(); repository_root = (repository_root or Path(".")).resolve()
    runtime = _json(run_root / "control/runtime_status.json")
    portfolio = _json(run_root / "control/portfolio_state.json")
    allocations = _json(run_root / "control/allocations/manifest.json")
    ledger_path = run_root / "ledger/execution.jsonl"
    ledger = summarize_ledger(ledger_path)
    strategy_net_pnl = {
        str(name): _number(row.get("final_pnl"))
        for name, row in (ledger.get("strategies") or {}).items()
        if isinstance(row, dict)
    }
    canonical = {
        "schema": "polymarket_v7_runtime_ledger_economics_v1",
        "generated_ts_ms": now * 1000,
        "ledger_valid": ledger.get("valid") is True,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "expected_model_sha": str(runtime.get("model_sha") or ""),
        "net_pnl": _number((ledger.get("total") or {}).get("final_pnl")),
        "strategy_net_pnl": strategy_net_pnl,
        "submitted_units": _integer((ledger.get("total") or {}).get("orders_submitted")),
        "complete_units": _integer((ledger.get("total") or {}).get("complete_fills")),
        "source": "CANONICAL_LEDGER_READ_ONLY",
        "model_families_observed": ledger.get("model_families_observed", []),
    }
    maker = _json(run_root / "micro_maker/status.json")
    native_manager = _json(run_root / "control/native_engine_manager_status.json")
    native_mode = (
        native_manager.get("schema") == "polymarket_v7_native_engine_manager_status_v1"
        and native_manager.get("paper_only") is True
        and native_manager.get("authenticated_execution") is False
        and native_manager.get("real_order_submission") is False
    )
    sha, runtime_sha = _git_head(repository_root), str(runtime.get("model_sha") or "")
    directives = _json(repository_root / "config/operator_directives.json")
    authorization = directives.get("paper_v7_authorization") if isinstance(directives.get("paper_v7_authorization"), dict) else {}
    max_drawdown = _number(authorization.get("max_drawdown"))
    authority_valid = directives.get("authority") == "latest_explicit_user_instruction" and authorization.get("paper_only") is True and authorization.get("authenticated_execution") is False and 0 < max_drawdown <= 1
    process_path = repository_root / "config/v7_process_manifest.json"
    process = _json(process_path)
    external_fair = summarize_external_fair(run_root, repository_root, runtime_sha=runtime_sha, now_s=now)
    lead_lag = _lead_lag_state(run_root, runtime_sha)
    model_families = canonical.get("model_families_observed")
    model_families = model_families if isinstance(model_families, list) else []
    lead_lag_required = "lead_lag_taker_v1" in model_families
    if native_mode:
        native_pnl = _optional_number(canonical.get("net_pnl"))
        state_pnl = {"CRYPTO_SETTLEMENT_ENGINE": native_pnl}
        state_pnl_components = {
            "CRYPTO_SETTLEMENT_ENGINE": {
                "native_canonical_ledger": native_pnl,
                "external_fair": None,
                "lead_lag_taker_v1": None,
                "lead_lag_required_by_canonical": False,
                "complete": native_pnl is not None,
                "total": native_pnl,
            },
        }
    else:
        external_pnl = _optional_number((external_fair.get("economics") or {}).get("realized_pnl"))
        crypto_state_pnl: float | None = external_pnl
        if lead_lag_required or lead_lag["present"]:
            if external_pnl is None or not lead_lag["valid"]:
                crypto_state_pnl = None
            else:
                crypto_state_pnl = external_pnl + float(lead_lag["realized_pnl"])
        state_pnl = {"CRYPTO_SETTLEMENT_ENGINE": crypto_state_pnl}
        state_pnl_components = {
            "CRYPTO_SETTLEMENT_ENGINE": {
                "external_fair": external_pnl,
                "lead_lag_taker_v1": lead_lag["realized_pnl"] if lead_lag["valid"] else None,
                "lead_lag_required_by_canonical": lead_lag_required,
                "complete": crypto_state_pnl is not None,
                "total": crypto_state_pnl,
            },
        }
    reconciliation = reconcile_portfolio(canonical=canonical, ledger=ledger, portfolio=portfolio, allocations=allocations, state_realized_pnl=state_pnl)
    engine_rows = portfolio.get("engines") if isinstance(portfolio.get("engines"), dict) else {}
    algorithms = {engine: {"equity": _number((engine_rows.get(engine) or {}).get("equity")), "budget": _number((engine_rows.get(engine) or {}).get("budget")), "killed": bool((engine_rows.get(engine) or {}).get("killed"))} for engine in LIVE_ALGORITHMS}
    tape = _trade_tape(run_root / "trade_tape.csv", now)
    starting = _number(portfolio.get("account_starting_capital"), _number(allocations.get("account_starting_capital")))
    equity = _number(portfolio.get("equity"), starting)
    snapshot = {
        "timestamp": now, "sha": sha, "run_root": run_root.name, "runtime": runtime,
        "runtime_alive": _pid_alive(runtime.get("pid")) and not (run_root / "control/KILL").exists(),
        "portfolio": portfolio, "allocations": allocations,
        "fee_reward_registry": _json(run_root / "control/fee_reward_registry.json"),
        "strategy_registry": _json(repository_root / "config/v7_strategy_registry.json"),
        "live_model_scope": _json(repository_root / "config/v7_live_model_scope.json"),
        "crypto_registry": _json(repository_root / "config/v7_crypto_settlement_markets.json"),
        "crypto_model_registry": _json(repository_root / "config/v7_crypto_settlement_model_registry.json"),
        "crypto_runtime": _json(run_root / "control/crypto_settlement_engine_snapshot.json"),
        "global_coordinator": _json(run_root / "control/global_portfolio_coordinator.json"),
        "native_engine_manager": native_manager,
        "research_training": _json(run_root / "control/research_training_status.json"),
        "native_mode": native_mode,
        "process_manifest": {
            "schema": process.get("schema"),
            "process_count": len(process.get("processes") or []),
            "expected_process_count": int(process.get("expected_process_count") or 0),
            "expected_launcher_child_count": int(process.get("expected_launcher_child_count") or 0),
            "sha256": hashlib.sha256(process_path.read_bytes()).hexdigest() if process_path.exists() else "",
        },
        "maker": maker,
        "lead_lag": _json(run_root / "research/lead_lag_taker_v1/status.json"),
        "lead_lag_collector": _json(run_root / "external_fair/lead_lag_collector_status.json"),
        "maker_diagnostics": _json(run_root / "micro_maker/runtime_diagnostics.json"),
        "maker_selector": _json(run_root / "micro_maker/selector_status.json"),
        "maker_rotation": _json(run_root / "micro_maker/rotation_status.json"),
        "external": _json(run_root / "external/status.json"),
        "universe": _json(run_root / "universe/status.json"),
        "book_data": _json(run_root / "research/repricing_book/fillability_ws_status.json"),
        "canonical_economics": canonical, "ledger": ledger,
        "research_plane": {"state": "OFF_LONDON", "runtime_training": False,
            "retrospective_analytics": False},
        "maker_lab": summarize_maker_microstructure(ledger_path, run_root / "micro_maker/reward_selection.json", run_root / "research/evidence/maker_markout"),
        "maker_fillability": _fillability(run_root, repository_root, runtime_sha, now),
        "external_fair": external_fair,
        "external_asset_data": _json(run_root / "external_fair/all_assets_status.json"),
        "reconciliation": reconciliation,
        "maker_latency": _runtime_latency(run_root),
        "lead_lag_summary": lead_lag, "state_realized_pnl_components": state_pnl_components,
        "trade_tape": tape, "trade_recorder": _trade_recorder(run_root / "trade_recorder_status.json", now),
        "authority": {"valid": authority_valid, "max_drawdown": max_drawdown},
        "algorithms": algorithms, "strategies": algorithms,
        "ages": {"economics": 0.0, "runtime": _age(now, runtime.get("timestamp")), "portfolio": _age(now, portfolio.get("timestamp")), "trade_tape": tape["age"]},
        "operations": _operations(run_root, runtime, None if live_observation_clock else now),
        "economics": {"starting_capital": starting, "cash": _number(allocations.get("reserve_budget")), "equity": equity, "pnl": equity-starting, "realized_pnl": canonical.get("net_pnl"), "unrealized_executable_pnl": equity-starting-_number(canonical.get("net_pnl")), "drawdown": _number(portfolio.get("drawdown")), "gross_exposure": 0.0, "capital_utilization": 0.0, "live_units": 0, "killed": bool(portfolio.get("killed")), "source": "LEDGER_PLUS_PORTFOLIO_GUARD"},
    }
    snapshot["multi_crypto_performance"] = summarize_multi_crypto(
        run_root, expected_sha=runtime_sha, portfolio=portfolio, canonical=canonical,
        global_coordinator=snapshot["global_coordinator"],
        crypto_registry=snapshot["crypto_registry"],
        crypto_model_registry=snapshot["crypto_model_registry"],
        ledger_valid=ledger.get("valid") is True,
        canonical_mtime_ms=(ledger_path.stat().st_mtime * 1000.0 if ledger_path.is_file() else None),
    )
    snapshot["multi_crypto_shadow"] = summarize_shadow_runtime(
        multi_crypto_shadow_run_root, now_ns=now * 1_000_000_000,
    )
    snapshot["pure_arb"] = _json(run_root / "research/repricing_book/pure_arb_status.json")
    snapshot["settlement_source_arb"] = _json(
        run_root / "research/repricing_book/settlement_source_arb_status.json")
    snapshot["complete_set_maker_shadow"] = _json(
        run_root / "research/repricing_book/two_sided_complete_set_status.json")
    snapshot["cross_market_exact_arb"] = _json(
        run_root / "research/repricing_book/cross_market_exact_arb_status.json")
    snapshot["unified_exact_arb_graph"] = _json(
        run_root / "research/repricing_book/unified_exact_arb_graph_status.json")
    snapshot["unified_exact_arb_graph_execution"] = _json(
        run_root / "research/repricing_book/unified_exact_arb_graph_execution_status.json")
    return snapshot


def _fresh_ms(status: dict[str, Any], snapshot: dict[str, Any], max_age: int) -> bool:
    age = _integer(snapshot.get("timestamp")) * 1000 - _integer(status.get("timestamp_ms"))
    return -5000 <= age <= max_age * 1000


def _scope_valid(snapshot: dict[str, Any]) -> bool:
    scope, registry = snapshot.get("live_model_scope") or {}, snapshot.get("strategy_registry") or {}
    rows = registry.get("live_algorithms") if isinstance(registry.get("live_algorithms"), list) else []
    ids = [row.get("id") for row in rows if isinstance(row, dict) and row.get("enabled") is True]
    return scope.get("schema") == "polymarket_v7_live_engine_scope_v2" and scope.get("live_algorithm_count") == 1 and set(scope.get("live_algorithms") or []) == set(LIVE_ALGORITHMS) and registry.get("schema") == "polymarket_v7_live_algorithm_registry_v2" and len(ids) == 1 and set(ids) == set(LIVE_ALGORITHMS) and scope.get("component_independent_authority") is False and registry.get("component_independent_authority") is False


def health_reasons(snapshot: dict[str, Any], *, max_runtime_age: int = 180, max_supervisor_age: int = 30) -> list[str]:
    reasons: list[str] = []
    runtime, portfolio, allocations = snapshot.get("runtime") or {}, snapshot.get("portfolio") or {}, snapshot.get("allocations") or {}
    canonical, ledger, ages = snapshot.get("canonical_economics") or {}, snapshot.get("ledger") or {}, snapshot.get("ages") or {}
    maker = snapshot.get("maker") or {}
    selector, rotation, universe = snapshot.get("maker_selector") or {}, snapshot.get("maker_rotation") or {}, snapshot.get("universe") or {}
    if not (snapshot.get("authority") or {}).get("valid"): reasons.append("operator_authority_missing_or_invalid")
    if runtime.get("version") != 7: reasons.append("runtime_version_not_v7")
    if runtime.get("paper_only") is not True: reasons.append("runtime_not_paper_only")
    if runtime.get("authenticated_execution") is not False or runtime.get("real_order_submission") is not False: reasons.append("authenticated_execution_not_disabled")
    if runtime.get("model_sha") != snapshot.get("sha"): reasons.append("runtime_sha_mismatch")
    if set(runtime.get("economic_engines") or []) != set(LIVE_ALGORITHMS): reasons.append("runtime_live_algorithms_not_crypto_only")
    if runtime.get("economic_new_risk_ready") is not False: reasons.append("economic_new_risk_must_remain_disabled")
    if runtime.get("authorized_alpha_actions") not in (None, []): reasons.append("authorized_alpha_actions_not_empty")
    if not _scope_valid(snapshot): reasons.append("live_algorithm_scope_missing_or_invalid")
    process_manifest = snapshot.get("process_manifest") or {}
    expected_process_count = _integer(process_manifest.get("expected_process_count"))
    if expected_process_count <= 0 or _integer(process_manifest.get("process_count")) != expected_process_count:
        reasons.append("process_manifest_count_mismatch")
    budgets = allocations.get("engine_budgets") if isinstance(allocations.get("engine_budgets"), dict) else {}
    if allocations.get("schema") != "polymarket_v7_capital_allocation_v3" or set(budgets) != set(LIVE_ALGORITHMS) or allocations.get("engine_count") != 1 or allocations.get("paper_only") is not True or allocations.get("authenticated_execution") is not False or allocations.get("real_order_submission") is not False or allocations.get("real_capital_at_risk") is not False or allocations.get("capital_authority_owner_count") != 1: reasons.append("crypto_engine_allocation_missing_or_unsafe")
    engines = portfolio.get("engines") if isinstance(portfolio.get("engines"), dict) else {}
    if portfolio.get("schema") != "polymarket_v7_portfolio_guard_v2" or set(engines) != set(LIVE_ALGORITHMS) or portfolio.get("paper_only") is not True or portfolio.get("authenticated_execution") is not False or portfolio.get("real_order_submission") is not False or portfolio.get("real_capital_at_risk") is not False: reasons.append("portfolio_guard_contract_invalid")
    native = snapshot.get("native_engine_manager") or {}
    native_mode = snapshot.get("native_mode") is True
    if native_mode:
        allowed_native_states = {
            "STARTING", "RUNNING", "ENGINE_EXITED", "ROTATED_CLEAN",
            "WAITING_FOR_CANONICAL_MARKET", "WAITING_FOR_ROLLOVER",
            "SETTLING", "RECOVERING_SETTLEMENT",
        }
        try:
            native_pid = int(native.get("engine_pid") or 0)
        except (TypeError, ValueError, OverflowError):
            native_pid = 0
        if (
            native.get("schema") != "polymarket_v7_native_engine_manager_status_v1"
            or native.get("model_sha") != snapshot.get("sha")
            or native.get("paper_only") is not True
            or native.get("authenticated_execution") is not False
            or native.get("real_order_submission") is not False
            or native.get("real_capital_at_risk") is not False
            or native.get("single_native_hot_path") is not True
            or native.get("state") not in allowed_native_states
            or not _fresh_ms(native, snapshot, max_runtime_age)
        ):
            reasons.append("native_engine_manager_missing_stale_or_unsafe")
        if native.get("partitioned_native_workers") is True:
            workers = native.get("workers")
            expected = _integer(native.get("expected_context_count"))
            targets = _integer(native.get("target_context_count"))
            global_budget = _integer(native.get("global_budget_microdollars"))
            partition_total = _integer(native.get("partition_total_microdollars"))
            if (
                native.get("single_native_portfolio_owner") is not True
                or expected != 30 or targets != expected
                or not isinstance(workers, list) or len(workers) != expected
                or global_budget <= 0 or partition_total <= 0
                or partition_total > global_budget
            ):
                reasons.append("native_partition_coverage_incomplete")
            else:
                for worker in workers:
                    if not isinstance(worker, dict):
                        reasons.append("native_partition_worker_invalid")
                        continue
                    worker_state = str(worker.get("state") or "")
                    if worker_state == "RUNNING" and not _pid_alive(worker.get("engine_pid")):
                        reasons.append("native_partition_engine_process_not_alive")
                    elif worker_state == "SETTLING" and not _pid_alive(worker.get("settlement_pid")):
                        reasons.append("native_partition_settlement_process_not_alive")
                    elif worker_state not in {"RUNNING", "SETTLING"}:
                        reasons.append("native_partition_worker_not_ready")
        elif native.get("state") == "RUNNING" and (native_pid <= 0 or not _pid_alive(native_pid)):
            reasons.append("native_engine_process_not_alive")
        if native.get("blocker"):
            reasons.append("native_engine_manager_blocked")
    else:
        fees = snapshot.get("fee_reward_registry") or {}
        if fees.get("schema") != "polymarket_v7_fee_reward_registry_v1" or fees.get("model_sha") != snapshot.get("sha") or fees.get("paper_only") is not True or fees.get("authenticated_execution") is not False or fees.get("real_order_submission") is not False or fees.get("unknown_fee_policy") != "NON_EXECUTABLE" or fees.get("unknown_reward_policy") != "ZERO_EXPECTED_VALUE": reasons.append("fee_reward_registry_missing_or_unsafe")
        if maker.get("schema") != "polymarket_v7_professional_maker_status_v1" or maker.get("model_sha") != snapshot.get("sha") or maker.get("paper_only") is not True or maker.get("authenticated_execution") is not False or maker.get("real_order_submission") not in (None, False) or maker.get("killed") is True or maker.get("source") in (None, "", "not_started") or not _fresh_ms(maker, snapshot, max_runtime_age): reasons.append("professional_maker_missing_stale_or_unsafe")
        if selector.get("schema") != "polymarket_v7_maker_selector_status_v1" or selector.get("model_sha") != snapshot.get("sha") or selector.get("ready") is not True or selector.get("state") not in _MAKER_SELECTOR_OPERATIONAL_STATES or selector.get("paper_only") is not True or selector.get("authenticated_execution") is not False or selector.get("real_order_submission") is not False or not _fresh_ms(selector, snapshot, max_runtime_age): reasons.append("maker_selector_missing_stale_or_unsafe")
        if rotation.get("schema") != "polymarket_v7_maker_cohort_rotation_status_v1" or rotation.get("model_sha") != snapshot.get("sha") or rotation.get("state") not in _MAKER_ROTATION_OPERATIONAL_STATES or rotation.get("paper_only") is not True or rotation.get("authenticated_execution") is not False or rotation.get("real_order_submission") is not False or not _fresh_ms(rotation, snapshot, max_runtime_age): reasons.append("maker_cohort_supervisor_missing_stale_or_unsafe")
    if universe.get("schema") != "polymarket_v7_crypto_universe_status_v1" or universe.get("model_sha") != snapshot.get("sha") or universe.get("state") != "OPERATIONAL" or universe.get("discovery_exhaustive") is not True or universe.get("pagination_loop_guard_hit") is not False or universe.get("paper_only") is not True or universe.get("authenticated_execution") is not False or universe.get("real_order_submission") is not False or _integer(universe.get("eligible_markets")) <= 0 or not _fresh_ms(universe, snapshot, max_runtime_age): reasons.append("crypto_universe_missing_stale_or_unsafe")
    if universe.get("book_selection_state") != "READY" or _integer(universe.get("book_selection_contexts")) != 30 or _integer(universe.get("book_selection_tokens")) != 60:
        reasons.append("crypto_book_data_coverage_incomplete")
    book_data = snapshot.get("book_data") or {}
    expected_subscribed_markets = max(
        30, _integer(universe.get("book_selection_subscribed_markets"), 30))
    expected_subscribed_tokens = max(
        60, _integer(universe.get("book_selection_subscribed_tokens"), 60))
    if (
        book_data.get("schema") != "polymarket_v7_maker_fillability_ws_status_v1"
        or book_data.get("model_sha") != snapshot.get("sha")
        or book_data.get("paper_only") is not True
        or book_data.get("authenticated_execution") is not False
        or book_data.get("real_order_submission") is not False
        or book_data.get("state") != "running"
        or _integer(book_data.get("subscribed_markets")) != expected_subscribed_markets
        or _integer(book_data.get("subscribed_tokens")) != expected_subscribed_tokens
        or _integer(book_data.get("observed_markets")) != expected_subscribed_markets
        or _integer(book_data.get("observed_tokens")) != expected_subscribed_tokens
        or book_data.get("subscription_coverage_complete") is not True
        or book_data.get("evidence_complete") is not True
        or _integer(book_data.get("dropped_events")) != 0
        or not _fresh_ms(book_data, snapshot, max_runtime_age)
    ):
        reasons.append("crypto_book_data_runtime_incomplete")
    if snapshot.get("runtime_alive") is not True: reasons.append("execution_not_alive")
    if canonical.get("schema") != "polymarket_v7_runtime_ledger_economics_v1" or canonical.get("paper_only") is not True or canonical.get("authenticated_execution") is not False: reasons.append("runtime_ledger_economics_missing_or_unsafe")
    if canonical.get("expected_model_sha") != snapshot.get("sha"): reasons.append("runtime_ledger_economics_sha_mismatch")
    if not ledger.get("present"): reasons.append("canonical_ledger_missing")
    elif not ledger.get("valid"): reasons.append("canonical_ledger_invalid_or_mixed_sha")
    rows = _integer((snapshot.get("trade_tape") or {}).get("rows"))
    if rows <= 0 and not _verified_no_flow(snapshot.get("trade_recorder") or {}, max_runtime_age): reasons.append("trade_tape_empty_or_unverified_no_standard_clob_flow")
    if _number(ages.get("runtime"), math.inf) > max_runtime_age: reasons.append("runtime_stale")
    if rows > 0 and _number(ages.get("trade_tape"), math.inf) > max_runtime_age: reasons.append("trade_tape_stale")
    if _number(ages.get("portfolio"), math.inf) > max_supervisor_age: reasons.append("portfolio_guard_stale")
    if (snapshot.get("economics") or {}).get("killed"): reasons.append("runtime_killed")
    retention, operations = (snapshot.get("operations") or {}).get("retention") or {}, snapshot.get("operations") or {}
    if retention.get("schema") != "polymarket_v7_london_buffer_retention_status_v1" or retention.get("paper_only") is not True or _number(operations.get("retention_age"), math.inf) > 7200: reasons.append("london_buffer_retention_missing_or_stale")
    if retention.get("state") == "BUFFER_LIMIT_EXCEEDED_UNSYNCED_DATA_PRESERVED": reasons.append("london_buffer_limit_exceeded_unsynced_data_preserved")
    if retention.get("state") == "RETENTION_PARTIAL_FAILURE": reasons.append("london_buffer_retention_partial_failure")
    external_data = snapshot.get("external_asset_data") or {}
    if (
        external_data.get("schema") != "polymarket_v7_multi_asset_external_collector_v1"
        or external_data.get("model_sha") != snapshot.get("sha")
        or external_data.get("state") != "OPERATIONAL"
        or external_data.get("paper_only") is not True
        or external_data.get("authenticated_execution") is not False
        or external_data.get("real_order_submission") is not False
        or external_data.get("execution_authority") is not False
        or _integer(external_data.get("asset_count")) != 6
        or _integer(external_data.get("ready_assets")) != 6
        or bool(external_data.get("missing_assets"))
        or not _fresh_ms(external_data, snapshot, max_runtime_age)
    ):
        reasons.append("crypto_external_data_coverage_incomplete")
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
    native = snapshot.get("native_engine_manager") or {}
    scope_ok = _scope_valid(snapshot)
    lines = [
        _metric("polymarket_v7_health", not reasons), _metric("polymarket_v7_runtime_info", 1),
        _metric("polymarket_v7_runtime_identity_info", 1, {"sha": snapshot.get("sha", "unknown"), "run_id": runtime.get("run_id", "")}),
        _metric("polymarket_runtime_info", 1, {"adapter": "v7_native", "run_root": snapshot.get("run_root", "unknown"), "version": "v7"}),
        _metric("polymarket_v7_operator_authority_valid", (snapshot.get("authority") or {}).get("valid")), _metric("polymarket_v7_authority_max_drawdown_ratio", (snapshot.get("authority") or {}).get("max_drawdown")),
        _metric("polymarket_v7_paper_only_contract_ok", runtime.get("paper_only") is True and runtime.get("real_order_submission") is False), _metric("polymarket_v7_authenticated_execution_disabled", runtime.get("authenticated_execution") is False),
        _metric("polymarket_v7_exact_sha_ok", runtime.get("model_sha") == snapshot.get("sha")), _metric("polymarket_v7_execution_alive", snapshot.get("runtime_alive")), _metric("polymarket_v7_supervisor_alive", operations.get("supervisor_alive")), _metric("polymarket_v7_single_writer_ok", operations.get("single_writer")), _metric("polymarket_v7_ledger_writable", operations.get("ledger_writable")),
        _metric("polymarket_v7_runtime_uptime_seconds", operations.get("runtime_uptime")), _metric("polymarket_v7_restart_count_window", operations.get("restart_count")), _metric("polymarket_v7_disk_free_ratio", operations.get("disk_free_ratio")),
        _metric("polymarket_v7_live_algorithm_count", 1), _metric("polymarket_v7_live_algorithm_scope_wired", scope_ok), _metric("polymarket_v7_live_model_scope_wired", scope_ok), _metric("polymarket_v7_economic_new_risk_ready", runtime.get("economic_new_risk_ready")),
        _metric("polymarket_runtime_equity_usd", economics.get("equity")), _metric("polymarket_runtime_pnl_usd", economics.get("pnl")), _metric("polymarket_runtime_realized_pnl_usd", economics.get("realized_pnl")), _metric("polymarket_runtime_drawdown_ratio", economics.get("drawdown")), _metric("polymarket_runtime_killed", economics.get("killed")),
        _metric("polymarket_v7_canonical_submitted_units", canonical.get("submitted_units")), _metric("polymarket_v7_canonical_complete_units", canonical.get("complete_units")), _metric("polymarket_v7_ledger_valid", ledger.get("valid")), _metric("polymarket_v7_portfolio_reconciled", (snapshot.get("reconciliation") or {}).get("reconciled")), _metric("polymarket_v7_reconciliation_divergences", len((snapshot.get("reconciliation") or {}).get("reason_codes") or [])),
        _metric("polymarket_v7_trade_tape_rows", (snapshot.get("trade_tape") or {}).get("rows")), _metric("polymarket_v7_trade_tape_assets", (snapshot.get("trade_tape") or {}).get("assets")), _metric("polymarket_v7_trade_tape_no_standard_clob_flow", _verified_no_flow(snapshot.get("trade_recorder") or {}, 180)), _metric("polymarket_v7_latency_samples_present", (snapshot.get("maker_latency") or {}).get("present")),
        _metric("polymarket_v7_component_ready", (
            ("native_engine_manager_missing_stale_or_unsafe" not in reasons
             and "native_engine_process_not_alive" not in reasons
             and "native_engine_manager_blocked" not in reasons)
            if snapshot.get("native_mode") is True
            else "professional_maker_missing_stale_or_unsafe" not in reasons
        ), {"component": "professional_maker"}),
        _metric("polymarket_v7_native_engine_mode", snapshot.get("native_mode") is True),
        _metric("polymarket_v7_native_target_contexts", (snapshot.get("native_engine_manager") or {}).get("target_context_count")),
        _metric("polymarket_v7_native_expected_contexts", (snapshot.get("native_engine_manager") or {}).get("expected_context_count")),
        _metric("polymarket_v7_native_active_workers", (snapshot.get("native_engine_manager") or {}).get("active_worker_count")),
        _metric("polymarket_v7_native_partition_budget_usd", _number((snapshot.get("native_engine_manager") or {}).get("partition_budget_microdollars")) / 1_000_000.0),
        _metric("polymarket_v7_native_global_budget_usd", _number((snapshot.get("native_engine_manager") or {}).get("global_budget_microdollars")) / 1_000_000.0),
        _metric("polymarket_v7_native_missing_contexts", max(0, _integer((snapshot.get("native_engine_manager") or {}).get("expected_context_count")) - _integer((snapshot.get("native_engine_manager") or {}).get("active_worker_count")))),
        _metric("polymarket_v7_native_launch_retry_contexts", (snapshot.get("native_engine_manager") or {}).get("launch_retry_count")),
        _metric("polymarket_v7_native_observations_published", (snapshot.get("native_engine_manager") or {}).get("native_observations_published")),
        _metric("polymarket_v7_native_observations_written", (snapshot.get("native_engine_manager") or {}).get("native_observations_written")),
        _metric("polymarket_v7_native_observations_dropped", (snapshot.get("native_engine_manager") or {}).get("native_observations_dropped")),
        _metric("polymarket_v7_native_observations_queue_depth", (snapshot.get("native_engine_manager") or {}).get("native_observations_queue_depth")),
        _metric("polymarket_v7_native_slow_context_publications", (snapshot.get("native_engine_manager") or {}).get("slow_context_publications")),
        _metric("polymarket_v7_native_slow_context_failures", (snapshot.get("native_engine_manager") or {}).get("slow_context_failures")),
        _metric("polymarket_v7_native_evidence_workers", (snapshot.get("native_engine_manager") or {}).get("evidence_worker_count")),
        _metric("polymarket_v7_native_pending_settlements", (snapshot.get("native_engine_manager") or {}).get("pending_settlement_count")),
        _metric("polymarket_v7_native_settlement_blocked", (snapshot.get("native_engine_manager") or {}).get("settlement_blocked_count")),
        _metric("polymarket_v7_native_engine_operational", (
            snapshot.get("native_mode") is True
            and (snapshot.get("native_engine_manager") or {}).get("state") == "RUNNING"
            and _integer((snapshot.get("native_engine_manager") or {}).get("active_worker_count")) > 0
            and "native_partition_engine_process_not_alive" not in reasons
            and "native_partition_worker_not_ready" not in reasons
        )),
        _metric("polymarket_v7_native_engine_ready", (
            snapshot.get("native_mode") is True
            and (snapshot.get("native_engine_manager") or {}).get("state") == "RUNNING"
            and _integer((snapshot.get("native_engine_manager") or {}).get("active_worker_count"))
                == _integer((snapshot.get("native_engine_manager") or {}).get("expected_context_count"))
            and _integer((snapshot.get("native_engine_manager") or {}).get("launch_retry_count")) == 0
            and "native_engine_process_not_alive" not in reasons
            and "native_partition_engine_process_not_alive" not in reasons
            and "native_engine_manager_blocked" not in reasons
        )),
        _metric("polymarket_v7_maker_selector_ready", selector.get("ready") and selector.get("state") in _MAKER_SELECTOR_OPERATIONAL_STATES), _metric("polymarket_v7_maker_selector_fallback_active", selector.get("degraded")), _metric("polymarket_v7_maker_runtime_selection_pinned", selector.get("runtime_selection_pinned")), _metric("polymarket_v7_maker_candidate_rotation_pending", selector.get("candidate_rotation_pending")), _metric("polymarket_v7_maker_candidate_selected_markets", selector.get("candidate_selected_count")),
        _metric("polymarket_v7_maker_candidate_fresh_flow_eligible", selector.get("candidate_fresh_flow_eligible")), _metric("polymarket_v7_maker_candidate_sell_flow_30s_markets", selector.get("candidate_selected_with_sell_flow_30s")), _metric("polymarket_v7_maker_candidate_sell_flow_2m_markets", selector.get("candidate_selected_with_sell_flow_2m")), _metric("polymarket_v7_maker_candidate_max_last_sell_age_seconds", selector.get("candidate_max_last_sell_age_seconds")),
        _metric("polymarket_v7_maker_cohort_supervisor_ready", rotation.get("state") in _MAKER_ROTATION_OPERATIONAL_STATES), _metric("polymarket_v7_maker_cohort_rotations_total", rotation.get("rotation_count")), _metric("polymarket_v7_maker_rotation_candidate_confirmations", rotation.get("candidate_confirmations")), _metric("polymarket_v7_maker_rotation_required_confirmations", rotation.get("candidate_required_confirmations")), _metric("polymarket_v7_maker_rotation_cooldown_remaining_seconds", rotation.get("rotation_cooldown_remaining_seconds")), _metric("polymarket_v7_maker_paused_no_fresh_flow", rotation.get("fresh_flow_pause_active")),
        _metric("polymarket_v7_maker_feed_connected_workers", diagnostics.get("feed_connected_workers")), _metric("polymarket_v7_maker_feed_messages_total", diagnostics.get("feed_messages")), _metric("polymarket_v7_maker_decisions_total", diagnostics.get("decisions")), _metric("polymarket_v7_maker_quote_intents_total", diagnostics.get("quote_intents")), _metric("polymarket_v7_maker_rejected_positive_point_ev_total", diagnostics.get("rejected_positive_point_ev")), _metric("polymarket_v7_maker_best_rejected_point_ev_per_share", diagnostics.get("best_rejected_point_ev_per_share")),
        _metric("polymarket_v7_universe_discovered_markets", universe.get("discovered_markets")), _metric("polymarket_v7_universe_eligible_markets", universe.get("eligible_markets")), _metric("polymarket_v7_universe_skipped_markets", universe.get("skipped_markets")), _metric("polymarket_v7_universe_pages", universe.get("pages")), _metric("polymarket_v7_universe_scan_duration_milliseconds", universe.get("scan_duration_ms")), _metric("polymarket_v7_universe_discovery_exhaustive", universe.get("discovery_exhaustive")),
        _metric("polymarket_v7_book_data_contexts", universe.get("book_selection_contexts")),
        _metric("polymarket_v7_book_data_tokens", universe.get("book_selection_tokens")),
        _metric("polymarket_v7_book_data_ready", universe.get("book_selection_state") == "READY"),
        _metric("polymarket_v7_book_data_subscribed_markets", (snapshot.get("book_data") or {}).get("subscribed_markets")),
        _metric("polymarket_v7_book_data_subscribed_tokens", (snapshot.get("book_data") or {}).get("subscribed_tokens")),
        _metric("polymarket_v7_book_data_observed_markets", (snapshot.get("book_data") or {}).get("observed_markets")),
        _metric("polymarket_v7_book_data_observed_tokens", (snapshot.get("book_data") or {}).get("observed_tokens")),
        _metric("polymarket_v7_book_data_runtime_ready", (snapshot.get("book_data") or {}).get("subscription_coverage_complete") is True and (snapshot.get("book_data") or {}).get("evidence_complete") is True),
        _metric("polymarket_v7_external_data_assets", (snapshot.get("external_asset_data") or {}).get("asset_count")),
        _metric("polymarket_v7_external_data_ready_assets", (snapshot.get("external_asset_data") or {}).get("ready_assets")),
        _metric("polymarket_v7_external_data_ready", (snapshot.get("external_asset_data") or {}).get("state") == "OPERATIONAL" and _integer((snapshot.get("external_asset_data") or {}).get("ready_assets")) == 6),
    ]
    for row in (snapshot.get("external_asset_data") or {}).get("assets") or []:
        if not isinstance(row, dict):
            continue
        lines.append(_metric(
            "polymarket_v7_external_asset_data_ready",
            row.get("data_ready"),
            {"asset": row.get("asset") or ""},
        ))
    native_manager = snapshot.get("native_engine_manager") or {}
    slow_error = str(native_manager.get("slow_context_error") or "")
    lines.append(_metric("polymarket_v7_native_slow_context_error_info", 1, {"error": slow_error or "NONE"}))
    for worker in native_manager.get("workers") or []:
        if not isinstance(worker, dict):
            continue
        labels = {
            "asset": worker.get("asset") or "",
            "horizon": worker.get("horizon") or "",
            "context": worker.get("context") or "",
            "market_id": worker.get("market_id") or "",
        }
        state = str(worker.get("state") or "")
        lines.append(_metric("polymarket_v7_native_context_present", 1, labels))
        lines.append(_metric("polymarket_v7_native_context_running", state == "RUNNING", labels))
        lines.append(_metric("polymarket_v7_native_context_settling", state == "SETTLING", labels))
        lines.append(_metric(
            "polymarket_v7_native_context_budget_usd",
            _number(worker.get("budget_microdollars")) / 1_000_000.0,
            labels,
        ))
        lines.append(_metric(
            "polymarket_v7_native_context_engine_pid",
            worker.get("engine_pid"),
            labels,
        ))

    configured = set(runtime.get("economic_engines") or [])
    for engine in LIVE_ALGORITHMS: lines.append(_metric("polymarket_v7_economic_engine_configured", engine in configured, {"engine": engine}))
    for name, row in sorted((snapshot.get("algorithms") or {}).items()):
        for field, metric in (("equity", "equity_usd"), ("budget", "budget_usd"), ("killed", "killed")): lines.append(_metric(f"polymarket_v7_live_algorithm_{metric}", row.get(field), {"algorithm": name}))
    coordinator = snapshot.get("global_coordinator") or {}
    crypto_risk = coordinator.get("crypto_correlation_risk") if isinstance(coordinator.get("crypto_correlation_risk"), dict) else {}
    for source, metric in (("gross_crypto_exposure_usd", "polymarket_v7_crypto_gross_exposure_usd"),
                           ("net_directional_crypto_exposure_usd", "polymarket_v7_crypto_net_directional_exposure_usd"),
                           ("correlated_crypto_cluster_exposure_usd", "polymarket_v7_crypto_cluster_exposure_usd")):
        value = _optional_number(crypto_risk.get(source))
        if value is not None:
            lines.append(_metric(metric, value))
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
        count = _number((total.get("markout_count") or {}).get(horizon)); lines.append(_metric("polymarket_execution_mean_markout", _number(value)/count if count else None, {"horizon": horizon}))
    for stage, row in sorted(((snapshot.get("maker_latency") or {}).get("stages") or {}).items()):
        for percentile in ("p50", "p90", "p95", "p99", "p99_9", "max"): lines.append(_metric("polymarket_v7_latency_stage_nanoseconds", row.get(percentile), {"stage": stage, "percentile": percentile}))
    for source, present in sorted(((snapshot.get("maker_latency") or {}).get("sources") or {}).items()):
        lines.append(_metric("polymarket_v7_latency_source_present", present, {"source": source}))
    lines.append(_metric(
        "polymarket_v7_native_decision_observations_total",
        native.get("native_decision_observations"),
    ))
    lines.append(_metric(
        "polymarket_v7_native_accepted_decision_observations_total",
        native.get("native_accepted_decision_observations"),
    ))
    lines.append(_metric(
        "polymarket_v7_native_rejected_decision_observations_total",
        native.get("native_rejected_decision_observations"),
    ))
    for reason, count in sorted((native.get("native_decision_reason_counts") or {}).items()):
        lines.append(_metric(
            "polymarket_v7_native_decision_reason_total", count, {"reason": reason}
        ))
    for stage, count in sorted((native.get("native_signal_funnel") or {}).items()):
        lines.append(_metric("polymarket_v7_native_signal_funnel_total", count, {"stage": stage}))
    for field in ("probability_model_configured", "probability_evaluation_open"):
        lines.append(_metric("polymarket_v7_" + field, native.get(field)))
    training = snapshot.get("research_training") or {}
    if (training.get("schema") == "v7_daily_retraining_receipt_v1"
            and training.get("paper_only") is True
            and training.get("authenticated_execution") is False
            and training.get("real_order_submission") is False
            and training.get("automatic_promotion") is False):
        for field in ("rows", "markets", "new_rows", "new_markets"):
            lines.append(_metric("polymarket_v7_research_" + field, training.get(field)))
        cutoff = training.get("cutoff_ns")
        lines.append(_metric("polymarket_v7_research_training_cutoff_seconds",
                             cutoff / 1e9 if isinstance(cutoff, int) else None))
        lines.append(_metric("polymarket_v7_research_state_info", 1, {
            "state": training.get("candidate_state") or "UNKNOWN",
            "result": training.get("result") or "UNKNOWN",
            "artifact_sha256": training.get("artifact_sha256") or "UNAVAILABLE"}))
    for context, counts in sorted((native.get("native_context_decision_reason_counts") or {}).items()):
        asset, _, horizon = str(context).partition(":")
        for reason, count in sorted((counts or {}).items()):
            lines.append(_metric(
                "polymarket_v7_native_context_decision_reason_total",
                count,
                {"asset": asset or "UNKNOWN", "horizon": horizon or "UNKNOWN", "reason": reason},
            ))
    append_operator_metrics(lines, snapshot, reasons, _metric)
    _append_maker_metrics(lines, snapshot)
    from exporter_v7_fillability import _append_fillability_metrics
    from exporter_v7_external import _append_external_fair_metrics
    _append_fillability_metrics(lines, snapshot.get("maker_fillability") or {})
    _append_external_fair_metrics(lines, snapshot.get("external_fair") or {})
    lines.extend(render_multi_crypto_prometheus(snapshot.get("multi_crypto_performance") or {}))
    lines.extend(render_shadow_prometheus(snapshot.get("multi_crypto_shadow") or {}))
    lines.extend(_render_pure_arb_metrics(snapshot.get("pure_arb") or {}))
    lines.extend(_render_settlement_source_arb_metrics(snapshot.get("settlement_source_arb") or {}))
    lines.extend(_render_complete_set_maker_metrics(snapshot.get("complete_set_maker_shadow") or {}))
    lines.extend(_render_cross_market_exact_arb_metrics(snapshot.get("cross_market_exact_arb") or {}))
    lines.extend(_render_unified_exact_arb_graph_metrics(snapshot.get("unified_exact_arb_graph") or {}))
    lines.extend(_render_unified_exact_arb_graph_execution_metrics(snapshot.get("unified_exact_arb_graph_execution") or {}))
    retention=operations.get('retention') or {}
    storage=retention.get('hft_storage') or {}
    population=retention.get('hft_opportunity_preservation') or {}
    for key, value in {
        'managed_bytes':retention.get('after_bytes'),
        'raw_retention_hours':storage.get('raw_retention_hours'),
        'total_gb_per_hour':storage.get('total_gb_per_hour'),
        'estimated_compressed_raw_hours':storage.get('estimated_compressed_raw_hours'),
        'hours_until_ceiling':storage.get('hours_until_ceiling_without_compression'),
        'permanent_opportunities':population.get('total_opportunities'),
        'permanent_markets':population.get('total_markets'),
        'capture_failures':len(population.get('failures',[])),
    }.items():
        lines.append(_metric('polymarket_v7_hft_'+key,value))
    for family,value in (storage.get('gb_per_hour') or {}).items():
        lines.append(_metric('polymarket_v7_hft_source_gb_per_hour',value,{'source':family}))
    for family,value in (storage.get('verified_compression_ratios') or {}).items():
        lines.append(_metric('polymarket_v7_hft_compression_ratio',value,{'source':family}))
    return "\n".join(lines) + "\n"


def render_cached_prometheus(cached: dict[str, Any], *, max_snapshot_age: float = 45.0) -> bytes:
    """Expose cache liveness at request time, not frozen at the last success."""
    age = _number(cached.get("age_seconds"), math.inf)
    usable = bool(cached.get("ready")) and age <= max_snapshot_age
    base = cached.get("metrics", b"").decode("utf-8")
    lines = [line for line in base.splitlines()
             if not line.startswith("polymarket_v7_exporter_snapshot_refresh_errors_total ")]
    if not usable:
        lines = ["polymarket_v7_health 0" if line.startswith("polymarket_v7_health ") else line for line in lines]
    lines.extend([
        _metric("polymarket_v7_exporter_snapshot_usable", usable),
        _metric("polymarket_v7_exporter_snapshot_age_seconds", age),
        _metric("polymarket_v7_exporter_snapshot_refresh_errors_total", cached.get("refresh_errors", 0)),
        _metric("polymarket_v7_exporter_last_refresh_failed", bool(cached.get("last_error"))),
    ])
    return ("\n".join(lines) + "\n").encode("utf-8")


class SnapshotCache:
    def __init__(self, run_root: Path, repository_root: Path, *, refresh_seconds: float = 10.0, multi_crypto_shadow_run_root: Path | None = None, include_profit_experiment_report: bool = True) -> None:
        self.run_root, self.repository_root, self.multi_crypto_shadow_run_root, self.refresh_seconds, self.include_profit_experiment_report = Path(run_root), Path(repository_root), (Path(multi_crypto_shadow_run_root) if multi_crypto_shadow_run_root else None), max(1.0, float(refresh_seconds)), bool(include_profit_experiment_report); self._lock = threading.Lock(); self._ready = threading.Event(); self._stop = threading.Event(); self._thread = None; self._snapshot = None; self._metrics = b""; self._maker_fillability = b"{}\n"; self._external_fair = b"{}\n"; self._multi_crypto_performance = b"{}\n"; self._multi_crypto_shadow = b"{}\n"; self._pure_arb = b"{}\n"; self._completed_monotonic = 0.0; self._completed_wall = 0.0; self._refresh_duration = 0.0; self._refresh_errors = 0; self._last_error = ""
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
                kwargs: dict[str, Any] = {}
                if self.multi_crypto_shadow_run_root is not None:
                    kwargs["multi_crypto_shadow_run_root"] = self.multi_crypto_shadow_run_root
                if not self.include_profit_experiment_report:
                    kwargs["include_profit_experiment_report"] = False
                snapshot = collect_snapshot(self.run_root, self.repository_root, **kwargs)
                duration = time.monotonic()-started; wall = time.time(); metrics = render_prometheus(snapshot).rstrip()+f"\npolymarket_v7_exporter_snapshot_generated_unixtime {wall}\npolymarket_v7_exporter_snapshot_refresh_duration_seconds {duration}\npolymarket_v7_exporter_snapshot_refresh_errors_total {self._refresh_errors}\n"
                with self._lock: self._snapshot=snapshot; self._metrics=metrics.encode(); self._maker_fillability=(json.dumps(snapshot.get("maker_fillability") or {},sort_keys=True)+"\n").encode(); self._external_fair=(json.dumps(snapshot.get("external_fair") or {},sort_keys=True)+"\n").encode(); self._multi_crypto_performance=(json.dumps(snapshot.get("multi_crypto_performance") or {},sort_keys=True)+"\n").encode(); self._multi_crypto_shadow=(json.dumps(snapshot.get("multi_crypto_shadow") or {},sort_keys=True)+"\n").encode(); self._pure_arb=(json.dumps(snapshot.get("pure_arb") or {},sort_keys=True)+"\n").encode(); self._completed_monotonic=time.monotonic(); self._completed_wall=wall; self._refresh_duration=duration; self._last_error=""
                self._ready.set()
            except Exception as exc:
                with self._lock: self._refresh_errors += 1; self._last_error=f"{type(exc).__name__}:{exc}"
            self._stop.wait(max(.1, self.refresh_seconds-(time.monotonic()-started)))
    def read(self) -> dict[str, Any]:
        with self._lock: return {"ready":self._snapshot is not None,"snapshot":self._snapshot,"metrics":self._metrics,"maker_fillability":self._maker_fillability,"external_fair":self._external_fair,"multi_crypto_performance":self._multi_crypto_performance,"multi_crypto_shadow":self._multi_crypto_shadow,"pure_arb":self._pure_arb,"age_seconds":max(0,time.monotonic()-self._completed_monotonic) if self._completed_monotonic else math.inf,"completed_wall":self._completed_wall,"refresh_duration_seconds":self._refresh_duration,"refresh_errors":self._refresh_errors,"last_error":self._last_error}


class ExporterHandler(BaseHTTPRequestHandler):
    run_root=Path("runs/paper_v7_live"); repository_root=Path("."); multi_crypto_shadow_run_root: Path | None=None; include_profit_experiment_report=True; max_runtime_age=180; max_supervisor_age=30; max_snapshot_age=45.0; snapshot_cache: SnapshotCache | None=None
    def log_message(self,_format:str,*_args:object)->None: return
    def do_GET(self)->None:
        cached=self.snapshot_cache.read() if self.snapshot_cache else None
        if cached is None:
            kwargs: dict[str, Any] = {}
            if self.multi_crypto_shadow_run_root is not None: kwargs["multi_crypto_shadow_run_root"] = self.multi_crypto_shadow_run_root
            if not self.include_profit_experiment_report: kwargs["include_profit_experiment_report"] = False
            snapshot=collect_snapshot(self.run_root,self.repository_root,**kwargs); cached={"ready":True,"snapshot":snapshot,"metrics":render_prometheus(snapshot).encode(),"maker_fillability":(json.dumps(snapshot.get("maker_fillability") or {})+"\n").encode(),"external_fair":(json.dumps(snapshot.get("external_fair") or {})+"\n").encode(),"multi_crypto_performance":(json.dumps(snapshot.get("multi_crypto_performance") or {})+"\n").encode(),"multi_crypto_shadow":(json.dumps(snapshot.get("multi_crypto_shadow") or {})+"\n").encode(),"pure_arb":(json.dumps(snapshot.get("pure_arb") or {})+"\n").encode(),"age_seconds":0}
        if not cached.get("ready"): payload=b'{"ok":false,"reasons":["exporter_snapshot_not_ready"]}\n'; self.send_response(503); content="application/json"
        elif self.path=="/metrics": payload=render_cached_prometheus(cached,max_snapshot_age=self.max_snapshot_age); self.send_response(200); content="text/plain; version=0.0.4"
        elif self.path=="/healthz":
            reasons=health_reasons(cached["snapshot"],max_runtime_age=self.max_runtime_age,max_supervisor_age=self.max_supervisor_age)
            if _number(cached.get("age_seconds"),math.inf)>self.max_snapshot_age: reasons.append("exporter_snapshot_stale")
            reasons=sorted(set(reasons)); payload=(json.dumps({"ok":not reasons,"reasons":reasons},sort_keys=True)+"\n").encode(); self.send_response(200 if not reasons else 503); content="application/json"
        elif self.path=="/maker-fillability.json": payload=cached["maker_fillability"]; self.send_response(200); content="application/json"
        elif self.path=="/external-fair.json": payload=cached["external_fair"]; self.send_response(200); content="application/json"
        elif self.path=="/multi-crypto-performance.json": payload=cached["multi_crypto_performance"]; self.send_response(200); content="application/json"
        elif self.path=="/multi-crypto-shadow.json": payload=cached["multi_crypto_shadow"]; self.send_response(200); content="application/json"
        elif self.path=="/pure-arb.json": payload=cached["pure_arb"]; self.send_response(200); content="application/json"
        elif self.path=="/runtime-artifacts.json":
            value=_json(self.run_root / "control/runtime_artifact_receipt.json")
            payload=(json.dumps(value,sort_keys=True)+"\n").encode(); self.send_response(200); content="application/json"
        else: payload=b"not found\n"; self.send_response(404); content="text/plain"
        self.send_header("Content-Type",content+"; charset=utf-8"); self.send_header("Content-Length",str(len(payload))); self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--run-root",type=Path,default=Path("runs/paper_v7_live")); parser.add_argument("--repository-root",type=Path,default=Path(".")); parser.add_argument("--host",default="127.0.0.1"); parser.add_argument("--port",type=int,default=9108); parser.add_argument("--max-runtime-age",type=int,default=180); parser.add_argument("--max-supervisor-age",type=int,default=30); parser.add_argument("--snapshot-refresh-seconds",type=float,default=10); parser.add_argument("--max-snapshot-age",type=float,default=45); parser.add_argument("--multi-crypto-shadow-run-root",type=Path,default=None); parser.add_argument("--skip-profit-experiment-report",action="store_true",help="Do not preload the potentially large profit experiment report into each cached snapshot."); args=parser.parse_args()
    ExporterHandler.run_root=args.run_root; ExporterHandler.repository_root=args.repository_root; ExporterHandler.multi_crypto_shadow_run_root=args.multi_crypto_shadow_run_root; ExporterHandler.include_profit_experiment_report=not args.skip_profit_experiment_report; ExporterHandler.max_runtime_age=args.max_runtime_age; ExporterHandler.max_supervisor_age=args.max_supervisor_age; ExporterHandler.max_snapshot_age=args.max_snapshot_age
    cache=SnapshotCache(args.run_root,args.repository_root,refresh_seconds=args.snapshot_refresh_seconds,multi_crypto_shadow_run_root=args.multi_crypto_shadow_run_root,include_profit_experiment_report=not args.skip_profit_experiment_report); cache.start(); ExporterHandler.snapshot_cache=cache; server=ThreadingHTTPServer((args.host,args.port),ExporterHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); cache.stop()
    return 0


if __name__=="__main__": raise SystemExit(main())
