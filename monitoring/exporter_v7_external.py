#!/usr/bin/env python3
"""Canonical V7 exporter + maker fillability + settlement-aware external fair."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import exporter_v7_fillability as base
from v7_external_fair import summarize_external_fair


def collect_snapshot(run_root: Path, repository_root: Path | None = None, *, now: int | None = None) -> dict[str, Any]:
    snapshot = base.collect_snapshot(run_root, repository_root, now=now)
    repository_root = (repository_root or Path(".")).resolve()
    runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    runtime_sha = str(runtime.get("model_sha") or "")
    snapshot["external_fair"] = summarize_external_fair(
        Path(run_root),
        repository_root,
        runtime_sha=runtime_sha,
        now_s=now,
    )
    return snapshot


def _append_external_fair_metrics(lines: list[str], report: dict[str, Any]) -> None:
    metric = base.base._metric
    lines.extend([
        metric("polymarket_external_fair_present", 1 if report.get("present") else 0),
        metric("polymarket_external_fair_healthy", 1 if report.get("healthy") else 0),
        metric("polymarket_external_fair_shadow_zero_authority", 1 if report.get("shadow_zero_authority") else 0),
        metric("polymarket_external_fair_exact_sha_ok", 1 if report.get("exact_sha_ok") else 0),
        metric("polymarket_external_fair_paper_only", 1 if report.get("paper_only") else 0),
        metric("polymarket_external_fair_authenticated_execution_disabled", 0 if report.get("authenticated_execution") else 1),
        metric("polymarket_external_fair_real_order_submission_disabled", 0 if report.get("real_order_submission") else 1),
        metric("polymarket_external_fair_required_markets", report.get("external_fair_required_markets")),
        metric("polymarket_external_fair_authority_info", 1, {"authority": report.get("execution_authority", "UNKNOWN")}),
    ])

    contract = report.get("contract") if isinstance(report.get("contract"), dict) else {}
    reference = report.get("settlement_reference") if isinstance(report.get("settlement_reference"), dict) else {}
    lines.extend([
        metric("polymarket_external_fair_contract_verified", 1 if contract.get("verified") else 0),
        metric("polymarket_external_fair_rules_hash_recognized", 1 if contract.get("rules_hash_recognized") else 0),
        metric("polymarket_external_fair_oracle_window_seconds", contract.get("oracle_window_seconds")),
        metric("polymarket_external_fair_reference_valid", 1 if reference.get("valid") else 0),
        metric("polymarket_external_fair_reference_value", reference.get("value")),
        metric("polymarket_external_fair_reference_version", reference.get("version")),
    ])

    oracle = report.get("oracle") if isinstance(report.get("oracle"), dict) else {}
    lines.extend([
        metric("polymarket_external_fair_oracle_healthy", 1 if oracle.get("healthy") else 0),
        metric("polymarket_external_fair_oracle_value", oracle.get("value")),
        metric("polymarket_external_fair_oracle_age_seconds", float(oracle.get("age_ns") or 0) / 1e9),
        metric("polymarket_external_fair_oracle_connection_epoch", oracle.get("connection_epoch")),
        metric("polymarket_external_fair_oracle_reconnects", oracle.get("reconnects")),
        metric("polymarket_external_fair_oracle_gaps", oracle.get("gaps")),
        metric("polymarket_external_fair_oracle_continuity_info", 1, {"continuity": oracle.get("continuity", "CONTINUITY_UNKNOWN")}),
    ])

    external = report.get("external") if isinstance(report.get("external"), dict) else {}
    lines.extend([
        metric("polymarket_external_fair_external_healthy", 1 if external.get("healthy") else 0),
        metric("polymarket_external_fair_healthy_venue_count", external.get("fresh_venue_count")),
        metric("polymarket_external_fair_venue_dispersion_bps", external.get("dispersion_bps")),
        metric("polymarket_external_fair_external_age_seconds", float(external.get("age_ns") or 0) / 1e9),
    ])
    for venue in external.get("venues") if isinstance(external.get("venues"), list) else []:
        if not isinstance(venue, dict):
            continue
        labels = {"venue": venue.get("venue", "UNKNOWN")}
        lines.extend([
            metric("polymarket_external_fair_venue_healthy", 1 if venue.get("healthy") else 0, labels),
            metric("polymarket_external_fair_venue_age_seconds", float(venue.get("age_ns") or 0) / 1e9, labels),
            metric("polymarket_external_fair_venue_price", venue.get("price"), labels),
            metric("polymarket_external_fair_venue_microprice", venue.get("microprice"), labels),
            metric("polymarket_external_fair_venue_spread_bps", venue.get("spread_bps"), labels),
            metric("polymarket_external_fair_venue_weight", venue.get("weight"), labels),
            metric("polymarket_external_fair_venue_basis_bps", venue.get("basis_bps"), labels),
            metric("polymarket_external_fair_venue_actionable_lead_ms", venue.get("actionable_lead_ms"), labels),
            metric("polymarket_external_fair_venue_economic_lead_ms", venue.get("economic_lead_ms"), labels),
            metric("polymarket_external_fair_venue_disabled", 1 if venue.get("disabled") else 0, labels),
        ])

    fair = report.get("fair") if isinstance(report.get("fair"), dict) else {}
    lines.extend([
        metric("polymarket_external_fair_valid", 1 if fair.get("valid") else 0),
        metric("polymarket_external_fair_yes", fair.get("yes")),
        metric("polymarket_external_fair_yes_lower", fair.get("lower")),
        metric("polymarket_external_fair_yes_upper", fair.get("upper")),
        metric("polymarket_external_fair_probability_order_ok", 1 if fair.get("probability_order_ok") else 0),
        metric("polymarket_external_fair_structural_probability", fair.get("structural")),
        metric("polymarket_external_fair_calibrated_probability", fair.get("calibrated")),
        metric("polymarket_external_fair_micro_logit_adjustment", fair.get("micro_logit_adjustment")),
        metric("polymarket_external_fair_pm_mid", fair.get("pm_mid")),
        metric("polymarket_external_fair_pm_gap", float(fair.get("yes") or 0) - float(fair.get("pm_mid") or 0)),
        metric("polymarket_external_fair_tte_seconds", fair.get("tte_seconds")),
        metric("polymarket_external_fair_settlement_margin", fair.get("settlement_margin")),
        metric("polymarket_external_fair_settlement_sigma", fair.get("settlement_sigma")),
    ])

    actions = report.get("actions") if isinstance(report.get("actions"), dict) else {}
    for action in ("MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING"):
        lines.append(metric("polymarket_external_fair_actions_total", actions.get(action), {"action": action}))
    router = report.get("paper_router") if isinstance(report.get("paper_router"), dict) else {}
    lines.extend([
        metric("polymarket_external_fair_router_active_candidates", router.get("active_candidates")),
        metric("polymarket_external_fair_router_orders_submitted_total", router.get("orders_submitted")),
        metric("polymarket_external_fair_router_fills_total", router.get("fills")),
        metric("polymarket_external_fair_router_book_requests_total", router.get("book_requests")),
        metric("polymarket_external_fair_router_book_request_failures_total", router.get("book_request_failures")),
        metric("polymarket_external_fair_router_book_parse_failures_total", router.get("book_parse_failures")),
    ])
    for reason, count in sorted((router.get("rejection_reasons") or {}).items()):
        lines.append(metric("polymarket_external_fair_router_rejections_total", count, {"reason": reason}))
    last_decision = router.get("last_decision") if isinstance(router.get("last_decision"), dict) else {}
    lines.append(metric("polymarket_external_fair_router_last_decision_info", 1, {
        "outcome": last_decision.get("outcome", "UNKNOWN"),
    }))
    purposes = report.get("purposes") if isinstance(report.get("purposes"), dict) else {}
    for purpose in ("ALPHA", "INVENTORY_REDUCTION", "RISK", "LIQUIDATION"):
        lines.append(metric("polymarket_external_fair_action_purpose_total", purposes.get(purpose), {"purpose": purpose}))

    cancel = report.get("cancel") if isinstance(report.get("cancel"), dict) else {}
    for reason, key in (
        ("FAIR_SHOCK", "fair_shock"),
        ("ORACLE_INVALID", "oracle_invalid"),
        ("EXTERNAL_STATE_INVALID", "external_invalid"),
        ("UNCERTAINTY_SPIKE", "uncertainty_spike"),
    ):
        lines.append(metric("polymarket_external_fair_cancel_reason_total", cancel.get(key), {"reason": reason}))
    lines.extend([
        metric("polymarket_external_fair_cancel_latency_ms", cancel.get("latency_p50_ms"), {"quantile": "p50"}),
        metric("polymarket_external_fair_cancel_latency_ms", cancel.get("latency_p99_ms"), {"quantile": "p99"}),
        metric("polymarket_external_fair_stale_quote_exposure_ms", cancel.get("stale_exposure_ms")),
        metric("polymarket_external_fair_cancel_would_fill", cancel.get("would_fill")),
        metric("polymarket_external_fair_cancel_would_markout", cancel.get("would_markout")),
    ])

    economics = report.get("economics") if isinstance(report.get("economics"), dict) else {}
    for field, name in (
        ("maker_robust_ev", "polymarket_external_fair_maker_robust_ev"),
        ("taker_robust_ev", "polymarket_external_fair_taker_robust_ev"),
        ("realized_pnl", "polymarket_external_fair_realized_pnl"),
        ("terminal_pnl", "polymarket_external_fair_terminal_pnl"),
        ("taker_fees", "polymarket_external_fair_taker_fees"),
        ("maker_fees", "polymarket_external_fair_maker_fees"),
        ("maker_rebates", "polymarket_external_fair_maker_rebates"),
        ("liquidity_rewards", "polymarket_external_fair_liquidity_rewards"),
        ("slippage", "polymarket_external_fair_slippage"),
        ("markout", "polymarket_external_fair_markout"),
    ):
        lines.append(metric(name, economics.get(field)))

    model = report.get("model") if isinstance(report.get("model"), dict) else {}
    lines.extend([
        metric("polymarket_external_fair_model_mature", 1 if model.get("mature") else 0),
        metric("polymarket_external_fair_model_log_loss", model.get("log_loss")),
        metric("polymarket_external_fair_model_brier", model.get("brier")),
        metric("polymarket_external_fair_model_ece", model.get("ece")),
        metric("polymarket_external_fair_model_coverage", model.get("coverage")),
        metric("polymarket_external_fair_model_drift_score", model.get("drift_score")),
    ])
    for role in ("champion", "challenger"):
        pointer = model.get(role) if isinstance(model.get(role), dict) else {}
        lines.append(metric("polymarket_external_fair_model_info", 1 if pointer else 0, {
            "role": role.upper(),
            "version": pointer.get("model_version", ""),
            "hash": pointer.get("model_hash", ""),
        }))

    latency = report.get("latency") if isinstance(report.get("latency"), dict) else {}
    for stage, quantiles in sorted(latency.items()):
        if not isinstance(quantiles, dict):
            continue
        for quantile, value in sorted(quantiles.items()):
            lines.append(metric("polymarket_external_fair_latency_ms", value, {
                "stage": stage,
                "quantile": quantile.replace("_", "."),
            }))

    tape = report.get("tape") if isinstance(report.get("tape"), dict) else {}
    lines.extend([
        metric("polymarket_external_fair_tape_evidence_valid", 1 if tape.get("evidence_valid") else 0),
        metric("polymarket_external_fair_tape_accepted", tape.get("accepted")),
        metric("polymarket_external_fair_tape_written", tape.get("written")),
        metric("polymarket_external_fair_tape_dropped", tape.get("dropped")),
    ])
    for reason in report.get("hard_reasons") if isinstance(report.get("hard_reasons"), list) else []:
        lines.append(metric("polymarket_external_fair_hard_reason", 1, {"reason": reason}))
    blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []
    lines.append(metric("polymarket_external_fair_blockers", len(blockers)))
    for blocker in blockers:
        lines.append(metric("polymarket_external_fair_blocker", 1, {"blocker": blocker}))


def render_prometheus(snapshot: dict[str, Any]) -> str:
    lines = base.render_prometheus(snapshot).rstrip("\n").splitlines()
    report = snapshot.get("external_fair") if isinstance(snapshot.get("external_fair"), dict) else {}
    _append_external_fair_metrics(lines, report)
    return "\n".join(lines) + "\n"


def health_reasons(snapshot: dict[str, Any], *, max_runtime_age: int = 180, max_supervisor_age: int = 30) -> list[str]:
    reasons = base.health_reasons(
        snapshot,
        max_runtime_age=max_runtime_age,
        max_supervisor_age=max_supervisor_age,
    )
    report = snapshot.get("external_fair") if isinstance(snapshot.get("external_fair"), dict) else {}
    # Shadow failures are observable but do not stop BLUE. Active external-fair
    # hard reasons join the canonical health contract only after authority is no
    # longer shadow/zero and required markets exist.
    if report.get("external_fair_required_markets", 0) and not report.get("shadow_zero_authority", True):
        reasons.extend(str(reason) for reason in report.get("hard_reasons", []))
    return sorted(set(reasons))


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
            content_type = "text/plain; version=0.0.4; charset=utf-8"
        elif self.path == "/healthz":
            reasons = health_reasons(
                snapshot,
                max_runtime_age=self.max_runtime_age,
                max_supervisor_age=self.max_supervisor_age,
            )
            payload = (json.dumps({"ok": not reasons, "reasons": reasons}, sort_keys=True) + "\n").encode()
            self.send_response(200 if not reasons else 503)
            content_type = "application/json; charset=utf-8"
        elif self.path == "/maker-fillability.json":
            report = snapshot.get("maker_fillability") if isinstance(snapshot.get("maker_fillability"), dict) else {}
            payload = (json.dumps(report, sort_keys=True) + "\n").encode()
            self.send_response(200)
            content_type = "application/json; charset=utf-8"
        elif self.path == "/external-fair.json":
            report = snapshot.get("external_fair") if isinstance(snapshot.get("external_fair"), dict) else {}
            payload = (json.dumps(report, sort_keys=True) + "\n").encode()
            self.send_response(200)
            content_type = "application/json; charset=utf-8"
        else:
            payload = b"not found\n"
            self.send_response(404)
            content_type = "text/plain; charset=utf-8"
        self.send_header("Content-Type", content_type)
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
