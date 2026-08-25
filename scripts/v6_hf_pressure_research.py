#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def micro_diagnostics(state_path: Path, min_training_samples: int = 40) -> dict[str, Any]:
    state = read_json(state_path)
    samples = state.get("samples") if isinstance(state.get("samples"), list) else []
    labeled = [row for row in samples if isinstance(row, dict) and row.get("y") is not None]
    targets = [finite(row.get("y"), 0.0) for row in labeled]
    nonzero = sum(abs(value) > 1e-12 for value in targets)
    target_std = statistics.pstdev(targets) if len(targets) >= 2 else 0.0
    beta_raw = state.get("beta") if isinstance(state.get("beta"), list) else []
    beta = [finite(value, 0.0) for value in beta_raw]
    beta_l1 = sum(abs(value) for value in beta)
    enough = len(labeled) >= min_training_samples
    flat = enough and nonzero == 0 and target_std <= 1e-12 and beta_l1 <= 1e-12

    if flat:
        classification = "REJECT_TAKER_THRESHOLD_RELAXATION_FLAT_TARGET"
    elif enough and int(state.get("signals") or 0) == 0:
        classification = "NO_EXECUTABLE_TAKER_EDGE"
    elif enough:
        classification = "CAUSAL_MODEL_ACTIVE"
    else:
        classification = "WARMUP_INCOMPLETE"

    return {
        "classification": classification,
        "training_samples_required": min_training_samples,
        "labeled_samples": len(labeled),
        "nonzero_target_count": nonzero,
        "nonzero_target_fraction": (nonzero / len(labeled)) if labeled else 0.0,
        "target_std": target_std,
        "beta_l1": beta_l1,
        "signals_last_tick": int(state.get("signals") or 0),
        "opened_last_tick": int(state.get("opened") or 0),
        "best_edge_last_tick": finite(state.get("best_edge"), 0.0),
        "flat_causal_target": flat,
        "taker_threshold_relaxation_supported": not flat,
        "flat_fair_taker_bound": "if fair=mid, executable taker edge <= -0.5*spread - slippage - fees",
    }


def maker_diagnostics(run_dir: Path) -> dict[str, Any]:
    order_rows = read_csv(run_dir / "maker_order_log.csv")
    fill_rows = read_csv(run_dir / "maker_fills.csv")
    equity_rows = read_csv(run_dir / "maker_equity.csv")

    actions: dict[str, int] = {}
    for row in order_rows:
        action = (row.get("action") or "UNKNOWN").upper()
        actions[action] = actions.get(action, 0) + 1

    buy_fills = sum((row.get("action") or "").upper().startswith("BUY_MAKER") for row in fill_rows)
    sell_fills = sum((row.get("action") or "").upper().startswith("SELL_TAKER") for row in fill_rows)
    filled_shares = sum(
        max(0.0, finite(row.get("shares"), 0.0))
        for row in fill_rows
        if (row.get("action") or "").upper().startswith("BUY_MAKER")
    )

    first_equity = finite(equity_rows[0].get("equity"), math.nan) if equity_rows else math.nan
    last_equity = finite(equity_rows[-1].get("equity"), math.nan) if equity_rows else math.nan
    equity_delta = (
        last_equity - first_equity
        if math.isfinite(first_equity) and math.isfinite(last_equity)
        else None
    )
    final = equity_rows[-1] if equity_rows else {}
    ticks = len(equity_rows)
    posts = actions.get("POST", 0)

    return {
        "ticks": ticks,
        "posts": posts,
        "posts_per_tick": (posts / ticks) if ticks else 0.0,
        "queue_depletions": actions.get("QUEUE_DEPLETION", 0),
        "partial_fills": actions.get("PARTIAL_FILL", 0),
        "full_fills": actions.get("FILL", 0),
        "cancels_ttl": actions.get("CANCEL_TTL", 0),
        "cancels_stale": actions.get("CANCEL_STALE", 0),
        "maker_buy_fill_rows": buy_fills,
        "taker_exit_rows": sell_fills,
        "maker_filled_shares": filled_shares,
        "final_equity": last_equity if math.isfinite(last_equity) else None,
        "equity_delta_observed": equity_delta,
        "final_resting_orders": int(finite(final.get("resting_orders"), 0.0)),
        "final_positions": int(finite(final.get("positions"), 0.0)),
        "final_reserved_cash": finite(final.get("reserved_cash"), 0.0),
        "actions": actions,
    }


def build_report(
    micro_state: Path,
    maker_baseline: Path,
    maker_aggressive: Path,
    maker_broad: Path | None = None,
) -> dict[str, Any]:
    micro = micro_diagnostics(micro_state)
    baseline = maker_diagnostics(maker_baseline)
    aggressive = maker_diagnostics(maker_aggressive)
    broad = maker_diagnostics(maker_broad) if maker_broad is not None else None
    post_delta = aggressive["posts"] - baseline["posts"]
    fill_delta = aggressive["maker_buy_fill_rows"] - baseline["maker_buy_fill_rows"]

    if aggressive["ticks"] == 0:
        maker_state = "MISSING_CHALLENGER_EVIDENCE"
    elif post_delta > 0:
        maker_state = "SHADOW_MORE_EVIDENCE_REQUIRED"
    else:
        maker_state = "NO_ACTIVITY_IMPROVEMENT"

    report: dict[str, Any] = {
        "paper_only": True,
        "authenticated_execution": False,
        "micro_taker": micro,
        "maker_baseline": baseline,
        "maker_aggressive_threshold": aggressive,
        "maker_post_delta": post_delta,
        "maker_fill_row_delta": fill_delta,
        "maker_challenger_state": maker_state,
        "promotion_ready": False,
        "decision": (
            "Do not force taker fills when the causal target is flat. Keep taker admission cost-aware; "
            "test lower maker thresholds at a fixed universe separately from broader scanning, and require "
            "repeated queue fills, adverse markout, exits and stressed net PnL before any promotion."
        ),
    }
    if broad is not None:
        baseline_ticks = max(1, int(baseline["ticks"]))
        report["maker_broad_universe"] = broad
        report["broad_tick_ratio_vs_baseline"] = broad["ticks"] / baseline_ticks
        report["broad_scan_throughput_warning"] = bool(
            baseline["ticks"] >= 2 and broad["ticks"] < 0.75 * baseline["ticks"]
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose V6 HF zero-signal behavior and compare maker shadows")
    parser.add_argument("--micro-state", type=Path, required=True)
    parser.add_argument("--maker-baseline", type=Path, required=True)
    parser.add_argument("--maker-aggressive", type=Path, required=True)
    parser.add_argument("--maker-broad", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = build_report(args.micro_state, args.maker_baseline, args.maker_aggressive, args.maker_broad)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
