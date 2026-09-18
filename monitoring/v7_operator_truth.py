"""Read-only operator telemetry: absence is not an observed zero; no execution authority."""
from __future__ import annotations
import math
from typing import Any, Callable


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def prometheus_number(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "NaN"
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "NaN"
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    return f"{number:.12g}"


def source_status(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    now = finite(snapshot.get("timestamp"))
    sha = (snapshot.get("runtime") or {}).get("model_sha")
    specifications = (
        ("runtime", "runtime", ("polymarket_v7_runtime_status_v2", "polymarket_v7_runtime_status_v3"), "timestamp", 1, 180),
        ("portfolio", "portfolio", ("polymarket_v7_portfolio_guard_v2",), "timestamp", 1, 30),
        ("economics", "canonical_economics", ("polymarket_v7_canonical_economics_v1", "polymarket_v7_runtime_ledger_economics_v1"), None, 1, 180),
        ("lead_lag", "lead_lag", ("polymarket_v7_lead_lag_taker_v1_status",), "timestamp_ms", 1000, 30),
        ("lead_lag_collector", "lead_lag_collector", ("polymarket_v7_external_pm_lead_lag_collector_status_v1",), "timestamp_ns", 1e9, 30),
    )
    result = {}
    for name, key, schemas, time_key, scale, maximum in specifications:
        row = snapshot.get(key) or {}
        raw_time = finite(row.get(time_key)) if time_key else None
        age = now - raw_time / scale if now is not None and raw_time is not None and raw_time > 0 else math.inf
        if time_key is None:
            raw_age = finite((snapshot.get("ages") or {}).get(name))
            age = raw_age if raw_age is not None else math.inf
        valid = bool(row) and row.get("schema") in schemas and row.get("paper_only") is True and row.get("authenticated_execution") is False
        if name != "economics":
            valid = valid and row.get("real_order_submission") is False
        if name in {"runtime", "lead_lag", "lead_lag_collector"}:
            valid = valid and bool(sha) and row.get("model_sha") == sha
        if name == "runtime":
            valid = valid and sha == snapshot.get("sha")
        if name == "portfolio":
            valid = valid and all(finite(row.get(k)) is not None for k in ("equity", "account_starting_capital", "drawdown"))
        if name == "economics":
            valid = valid and bool(sha) and row.get("expected_model_sha") == sha
        if name == "lead_lag":
            valid = valid and row.get("automatic_promotion") is False
        result[name] = {"present": bool(row), "valid": bool(valid), "fresh": bool(valid and -5 <= age <= maximum), "age": max(0.0, age), "max_age": maximum}
    return result


def operator_summary(snapshot: dict[str, Any], runtime_reasons: list[str]) -> dict[str, Any]:
    sources = source_status(snapshot)
    reconciliation = snapshot.get("reconciliation") or {}
    ledger = snapshot.get("ledger") or {}
    current_sha = (snapshot.get("runtime") or {}).get("model_sha")
    ledger_current = bool(ledger.get("present") and ledger.get("valid") and set(ledger.get("model_shas") or []) <= {current_sha})
    verified = ledger_current and all(sources[k]["fresh"] for k in ("runtime", "portfolio", "economics")) and reconciliation.get("reconciled") is True
    verified = verified and finite((snapshot.get("canonical_economics") or {}).get("net_pnl")) is not None
    if snapshot.get("native_mode"):
        attribution = (snapshot.get("multi_crypto_performance") or {}).get("attribution") or {}
        verified = verified and attribution.get("reconciled") is True
    reasons = set(runtime_reasons)
    reasons.update(str(x) for x in reconciliation.get("reason_codes") or [])
    for name in ("runtime", "portfolio", "economics"):
        if not sources[name]["fresh"]:
            reasons.add(f"{name}_missing_invalid_or_stale")
    if not ledger_current:
        reasons.add("ledger_missing_invalid_or_wrong_runtime")
    if not verified:
        reasons.add("accounting_not_verified")
    operations = snapshot.get("operations") or {}
    for key in ("supervisor_alive", "single_writer", "ledger_writable"):
        if operations.get(key) is not True:
            reasons.add(key + "_not_verified")
    free = finite(operations.get("disk_free_ratio"))
    if free is None:
        reasons.add("disk_free_unknown")
    elif free < 0.10:
        reasons.add("disk_pressure")
    if not snapshot.get("native_mode") and sources["lead_lag"]["present"] and not sources["lead_lag"]["fresh"]:
        reasons.add("lead_lag_missing_invalid_or_stale")
    return {"sources": sources, "ledger_current": ledger_current, "accounting_verified": bool(verified), "attention_required": bool(reasons), "reasons": sorted(reasons)}


def append_operator_metrics(lines: list[str], snapshot: dict[str, Any], runtime_reasons: list[str], metric: Callable[..., str]) -> None:
    summary = operator_summary(snapshot, runtime_reasons)
    sources = summary["sources"]
    runtime = snapshot.get("runtime") or {}
    canonical = snapshot.get("canonical_economics") or {}
    total = (snapshot.get("ledger") or {}).get("total") or {}
    reconciliation = snapshot.get("reconciliation") or {}
    lines.extend([
        metric("polymarket_v7_operator_attention_required", summary["attention_required"]),
        metric("polymarket_v7_operator_reason_count", len(summary["reasons"])),
        metric("polymarket_v7_accounting_verified", summary["accounting_verified"]),
        metric("polymarket_v7_ledger_current_runtime", summary["ledger_current"]),
        metric("polymarket_v7_verified_net_pnl_usd", canonical.get("net_pnl") if summary["accounting_verified"] else None),
        metric("polymarket_v7_canonical_pnl_observed", finite(canonical.get("net_pnl")) is not None),
        metric("polymarket_v7_ledger_terminal_events", total.get("finals") if summary["ledger_current"] else None),
        metric("polymarket_v7_runtime_location_info", 1, {"server": runtime.get("server_id") or "unknown", "run_id": runtime.get("run_id") or "unknown", "sha": runtime.get("model_sha") or "unknown"}),
    ])
    for name, source in sources.items():
        for field in ("present", "valid", "fresh"):
            lines.append(metric(f"polymarket_v7_source_{field}", source[field], {"source": name}))
        lines.append(metric("polymarket_v7_source_age_seconds", source["age"], {"source": name}))
    for reason in summary["reasons"]:
        lines.append(metric("polymarket_v7_operator_reason", 1, {"reason": reason}))
    for strategy, row in (reconciliation.get("strategies") or {}).items():
        lines.append(metric("polymarket_v7_reconciliation_pnl_difference_usd", row.get("difference"), {"strategy": strategy}))
    for engine, row in ((snapshot.get("portfolio") or {}).get("engines") or {}).items():
        lines.append(metric("polymarket_v7_engine_reported_source_info", 1, {"engine": engine, "source": row.get("source") or "unknown"}))
    lead = snapshot.get("lead_lag") or {}
    valid = sources["lead_lag"]["valid"]
    lines.append(metric("polymarket_v7_lead_lag_status_info", 1, {"state": lead.get("state") if valid else "UNAVAILABLE", "mode": "PAPER_FORWARD_TEST"}))
    for field in ("entries", "settled", "wins", "open_positions", "candidate_count", "receipt_count", "arrival_rejections", "target_independent_markets"):
        lines.append(metric(f"polymarket_v7_lead_lag_{field}", lead.get(field) if valid else None))
    lines.append(metric("polymarket_v7_lead_lag_realized_pnl_usd", lead.get("realized_pnl") if valid else None))
    event = lead.get("last_event") or {}
    timestamp_ns = finite(event.get("timestamp_ns"))
    now = finite(snapshot.get("timestamp"))
    event_age = now - timestamp_ns / 1e9 if valid and timestamp_ns is not None and now is not None else None
    lines.append(metric("polymarket_v7_lead_lag_last_event_age_seconds", max(0, event_age) if event_age is not None else None))
    if valid and event:
        lines.append(metric("polymarket_v7_lead_lag_last_event_info", 1, {"event": event.get("event") or "UNKNOWN", "market": event.get("market_id") or "unknown", "outcome": event.get("outcome") or "unknown"}))
    if valid:
        for reason, count in sorted((lead.get("skip_reasons") or {}).items()):
            lines.append(metric("polymarket_v7_lead_lag_skip_checks_total", count, {"reason": reason}))
    collector = snapshot.get("lead_lag_collector") or {}
    for field in ("origins", "labels", "nominal_horizon_eligible_labels", "book_gap_censors", "invalid_reads", "pending_origins"):
        lines.append(metric(f"polymarket_v7_lead_lag_collector_{field}", collector.get(field) if sources["lead_lag_collector"]["valid"] else None))
    for horizon, count in sorted((total.get("markout_count") or {}).items()):
        lines.append(metric("polymarket_execution_markout_observations", count if summary["ledger_current"] else None, {"horizon": horizon}))
