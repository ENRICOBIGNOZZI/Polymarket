#!/usr/bin/env python3
"""Produce the required V7 settlement-aware forecasting/economic report.

The report deliberately separates forecasting, cancel overlay, maker repricing,
informed taker and combined-engine evidence. Missing forward evidence remains
missing; synthetic/unit-test data can never be labelled economic validation.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
MODEL_ORDER = (
    "PM Mid",
    "Oracle Only",
    "External Median",
    "Structural",
    "Structural + Bridge",
    "Full External",
    "Champion",
    "Challenger",
)
ACTIONS = ("MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING")
PURPOSES = ("ALPHA", "INVENTORY_REDUCTION", "RISK", "LIQUIDATION")
EDGE_BINS = (
    (0.0, 0.001),
    (0.001, 0.0025),
    (0.0025, 0.005),
    (0.005, 0.01),
    (0.01, 0.02),
    (0.02, 0.05),
    (0.05, math.inf),
)
PNL_FIELDS = (
    "gross_trading_pnl",
    "taker_fees",
    "maker_fees",
    "maker_rebates",
    "liquidity_rewards",
    "slippage",
    "adverse_selection",
    "latency_loss",
    "unwind_loss",
    "net_trading_pnl",
    "total_economic_pnl",
)


class ReportError(ValueError):
    pass


def _finite(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _load_json(path: Path | None) -> Any:
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read json {path}: {exc}") from exc


def _load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ReportError(f"{path}:{line_no}:not_object")
                rows.append(row)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read jsonl {path}: {exc}") from exc
    return rows


def forecasting_table(raw: Any) -> list[dict[str, Any]]:
    source = raw if isinstance(raw, dict) else {}
    models = source.get("models") if isinstance(source.get("models"), dict) else source
    output: list[dict[str, Any]] = []
    for name in MODEL_ORDER:
        value = models.get(name) if isinstance(models, dict) else None
        value = value if isinstance(value, dict) else {}
        scores = value.get("scores") if isinstance(value.get("scores"), dict) else value
        output.append({
            "model": name,
            "log_loss": scores.get("log_loss"),
            "brier": scores.get("brier"),
            "ece": scores.get("ece"),
            "calibration_slope": scores.get("calibration_slope"),
            "coverage": scores.get("coverage", value.get("coverage")),
            "independent_contracts": scores.get("contracts", value.get("independent_contracts")),
            "net_replay_pnl": value.get("net_replay_pnl"),
        })
    return output


def _edge_label(lower: float, upper: float) -> str:
    return f"[{lower:.4f},inf)" if math.isinf(upper) else f"[{lower:.4f},{upper:.4f})"


def robust_edge_table(opportunities: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(opportunities)
    output: list[dict[str, Any]] = []
    for lower, upper in EDGE_BINS:
        selected = []
        for row in rows:
            edge = _finite(row.get("robust_edge_per_share"), math.nan)
            if math.isfinite(edge) and edge >= lower and edge < upper:
                selected.append(row)
        contracts = {str(row.get("contract_id")) for row in selected if row.get("contract_id")}
        requested = sum(max(0.0, _finite(row.get("requested_quantity"))) for row in selected)
        filled = sum(max(0.0, _finite(row.get("filled_quantity"))) for row in selected)
        pnl = sum(_finite(row.get("realized_net_pnl")) for row in selected)
        per_share_denom = filled if filled > 0.0 else 0.0
        output.append({
            "robust_edge_bin": _edge_label(lower, upper),
            "independent_contracts": len(contracts),
            "opportunities": len(selected),
            "fill_rate": (filled / requested if requested > 0.0 else None),
            "pnl_per_share": (pnl / per_share_denom if per_share_denom > 0.0 else None),
            "net_pnl": pnl,
        })
    return output


def cancel_table(cancels: Iterable[dict[str, Any]], maker_evidence: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in cancels:
        grouped[str(row.get("reason") or "UNKNOWN").upper()].append(row)
    mature = str(maker_evidence).upper() == "MATURE"
    output: list[dict[str, Any]] = []
    for reason in sorted(grouped):
        rows = grouped[reason]
        would_fill_rows = [row for row in rows if row.get("would_fill") is True]
        markouts = [
            _finite(row.get("would_markout"), math.nan)
            for row in would_fill_rows
            if math.isfinite(_finite(row.get("would_markout"), math.nan))
        ]
        avoided = sum(_finite(row.get("estimated_loss_avoided")) for row in rows) if mature else None
        output.append({
            "cancel_reason": reason,
            "n": len(rows),
            "would_fill": len(would_fill_rows),
            "avg_would_markout": (sum(markouts) / len(markouts) if markouts else None),
            "estimated_loss_avoided": avoided,
            "counterfactual_cold_start": not mature,
        })
    return output


def action_table(actions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in actions:
        action = str(row.get("action") or "NOTHING").upper()
        purpose = str(row.get("purpose") or "ALPHA").upper()
        if action not in ACTIONS:
            raise ReportError(f"unsupported action {action}")
        if purpose not in PURPOSES:
            raise ReportError(f"unsupported purpose {purpose}")
        grouped[(action, purpose)].append(row)
    output: list[dict[str, Any]] = []
    for action in ACTIONS:
        for purpose in PURPOSES:
            rows = grouped.get((action, purpose), [])
            output.append({
                "action": action,
                "purpose": purpose,
                "count": len(rows),
                "expected_ev": sum(_finite(row.get("expected_ev")) for row in rows),
                "realized_pnl": sum(_finite(row.get("realized_pnl")) for row in rows),
                "counterfactual_value": sum(_finite(row.get("counterfactual_value")) for row in rows),
            })
    return output


def pnl_decomposition(raw: Any) -> dict[str, float]:
    source = raw if isinstance(raw, dict) else {}
    return {name: _finite(source.get(name)) for name in PNL_FIELDS}


def _independent_contracts(*row_sets: Iterable[dict[str, Any]]) -> int:
    ids: set[str] = set()
    for rows in row_sets:
        for row in rows:
            if row.get("contract_id"):
                ids.add(str(row["contract_id"]))
    return len(ids)


def build_report(
    *,
    forecasting: Any = None,
    opportunities: Iterable[dict[str, Any]] = (),
    cancels: Iterable[dict[str, Any]] = (),
    actions: Iterable[dict[str, Any]] = (),
    pnl: Any = None,
    maker_execution_evidence: str = "COLD_START",
    forward_shadow_contracts: int = 0,
    synthetic_test_only: bool = False,
) -> dict[str, Any]:
    opportunities = list(opportunities)
    cancels = list(cancels)
    actions = list(actions)
    forecast_rows = forecasting_table(forecasting)
    independent = _independent_contracts(opportunities, cancels, actions)
    maker_state = str(maker_execution_evidence or "COLD_START").upper()
    if maker_state not in {"COLD_START", "LEARNING", "MATURE"}:
        raise ReportError("maker_execution_evidence invalid")

    economic_validation = bool(
        not synthetic_test_only
        and forward_shadow_contracts > 0
        and independent > 0
        and any(row.get("net_replay_pnl") is not None for row in forecast_rows)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "synthetic_test_only": bool(synthetic_test_only),
        "economic_validation_state": "EVIDENCE_AVAILABLE" if economic_validation else "NOT_VALIDATED",
        "independent_contracts_observed": independent,
        "forward_shadow_contracts": max(0, int(forward_shadow_contracts)),
        "maker_execution_evidence": maker_state,
        "A_forecasting": {
            "table": forecast_rows,
            "claim": "proper scoring/calibration only; no execution claim",
        },
        "B_cancel_overlay": {
            "table": cancel_table(cancels, maker_state),
            "maker_counterfactual_precision": (
                "fill-conditioned" if maker_state == "MATURE" else "COLD_START_DO_NOT_CLAIM_PRECISE_AVOIDED_PNL"
            ),
        },
        "C_maker_repricing": {
            "execution_evidence": maker_state,
            "economically_promotable": maker_state == "MATURE" and economic_validation,
        },
        "D_informed_taker": {
            "robust_edge_table": robust_edge_table(opportunities),
            "economic_validation": economic_validation,
        },
        "E_combined_engine": {
            "action_table": action_table(actions),
            "pnl_decomposition": pnl_decomposition(pnl),
            "economic_validation": economic_validation,
        },
    }


def _fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# V7 Settlement-Aware External Fair — Economic Evidence",
        "",
        f"Economic validation: **{report['economic_validation_state']}**",
        f"Maker execution evidence: **{report['maker_execution_evidence']}**",
        f"Forward shadow contracts: **{report['forward_shadow_contracts']}**",
        "",
        "## A — Forecasting",
        "",
        "| Model | LogLoss | Brier | ECE | Cal Slope | Coverage | Contracts | Net Replay PnL |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["A_forecasting"]["table"]:
        lines.append("| " + " | ".join([
            row["model"], _fmt(row["log_loss"]), _fmt(row["brier"]), _fmt(row["ece"]),
            _fmt(row["calibration_slope"]), _fmt(row["coverage"]),
            _fmt(row["independent_contracts"]), _fmt(row["net_replay_pnl"]),
        ]) + " |")

    lines += [
        "", "## B — Cancel overlay", "",
        "| Cancel Reason | N | Would Fill | Avg Would-Markout | Estimated Loss Avoided | Cold Start |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["B_cancel_overlay"]["table"]:
        lines.append("| " + " | ".join([
            row["cancel_reason"], _fmt(row["n"]), _fmt(row["would_fill"]),
            _fmt(row["avg_would_markout"]), _fmt(row["estimated_loss_avoided"]),
            _fmt(row["counterfactual_cold_start"]),
        ]) + " |")

    lines += [
        "", "## C — Maker repricing", "",
        f"Execution evidence: **{report['C_maker_repricing']['execution_evidence']}**. ",
        f"Economically promotable: **{_fmt(report['C_maker_repricing']['economically_promotable'])}**.",
        "", "## D — Informed taker", "",
        "| Robust Edge | Independent Contracts | Opportunities | Fill Rate | PnL/Share | Net PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["D_informed_taker"]["robust_edge_table"]:
        lines.append("| " + " | ".join([
            row["robust_edge_bin"], _fmt(row["independent_contracts"]),
            _fmt(row["opportunities"]), _fmt(row["fill_rate"]),
            _fmt(row["pnl_per_share"]), _fmt(row["net_pnl"]),
        ]) + " |")

    lines += [
        "", "## E — Combined engine", "",
        "| Action | Purpose | Count | Expected EV | Realized PnL | Counterfactual |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in report["E_combined_engine"]["action_table"]:
        if row["count"] == 0:
            continue
        lines.append("| " + " | ".join([
            row["action"], row["purpose"], _fmt(row["count"]),
            _fmt(row["expected_ev"]), _fmt(row["realized_pnl"]),
            _fmt(row["counterfactual_value"]),
        ]) + " |")
    lines += ["", "### PnL decomposition", ""]
    for key, value in report["E_combined_engine"]["pnl_decomposition"].items():
        lines.append(f"- `{key}`: {_fmt(value)}")
    if report["synthetic_test_only"]:
        lines += ["", "> Synthetic/unit-test evidence only. It is not alpha or promotion evidence."]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forecasting", type=Path)
    parser.add_argument("--opportunities", type=Path)
    parser.add_argument("--cancels", type=Path)
    parser.add_argument("--actions", type=Path)
    parser.add_argument("--pnl", type=Path)
    parser.add_argument("--maker-execution-evidence", default="COLD_START")
    parser.add_argument("--forward-shadow-contracts", type=int, default=0)
    parser.add_argument("--synthetic-test-only", action="store_true")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(
        forecasting=_load_json(args.forecasting),
        opportunities=_load_jsonl(args.opportunities),
        cancels=_load_jsonl(args.cancels),
        actions=_load_jsonl(args.actions),
        pnl=_load_json(args.pnl),
        maker_execution_evidence=args.maker_execution_evidence,
        forward_shadow_contracts=args.forward_shadow_contracts,
        synthetic_test_only=args.synthetic_test_only,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output_markdown.write_text(to_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
