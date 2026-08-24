from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from validate_trade_recorder_health import evaluate as evaluate_trade_recorder_health
from validate_trade_recorder_health import parse_status_line as parse_trade_recorder_status

METRIC = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+0-9.eE]+)$")
SELECTED = {
    "polymarket_runtime_equity_usd",
    "polymarket_runtime_pnl_usd",
    "polymarket_runtime_drawdown_ratio",
    "polymarket_runtime_kill_switch",
    "polymarket_runtime_live_units",
    "polymarket_runtime_reserved_cash_usd",
    "polymarket_runtime_gross_exposure_usd",
    "polymarket_runtime_realized_pnl_usd_total",
    "polymarket_runtime_execution_imbalance_ratio",
    "polymarket_runtime_execution_staleness_seconds",
    "polymarket_runtime_oos_trades",
    "polymarket_runtime_oos_net_pnl_usd",
    "polymarket_runtime_oos_stressed_net_pnl_usd",
    "polymarket_runtime_oos_drawdown_ratio",
    "polymarket_runtime_oos_bootstrap_pvalue",
    "polymarket_runtime_oos_eligible",
    "polymarket_runtime_production_threshold",
    "polymarket_runtime_oos_staleness_seconds",
}
LOGS = {
    "trade_recorder": "trade_recorder_latest.log",
    "structural": "structural_latest.log",
    "rewards": "reward_latest.log",
    "b1": "stat_arb_pairs_latest.log",
    "b2": "stat_arb_pca_latest.log",
    "b2_coherence": "coherent_hedges.log",
    "multileg": "multileg_latest.log",
    "maker": "maker_latest.log",
    "terminal": "terminal_latest.log",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []


def metric_snapshot(path: Path) -> dict:
    out: dict[str, float | dict] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = METRIC.match(raw.strip())
        if not match:
            continue
        name, labels, value = match.groups()
        if name == "polymarket_runtime_info":
            out[name] = {"labels": labels or "", "value": float(value)}
        elif name in SELECTED:
            out[name] = float(value)
    return out


def tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:]


def fnum(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def inum(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def top_rows(path: Path, edge_key: str, limit: int = 8) -> list[dict[str, str]]:
    rows = read_rows(path)
    rows.sort(key=lambda row: fnum(row.get(edge_key), float("-inf")), reverse=True)
    return rows[:limit]


def positive_count(rows: list[dict[str, str]], key: str) -> int:
    return sum(fnum(row.get(key), float("-inf")) > 0.0 for row in rows)


def b2_coherence_summary(run_root: Path) -> dict:
    coherent = read_rows(run_root / "stat_arb_pca.csv")
    raw_path = run_root / "stat_arb_pca_raw.csv"
    raw = read_rows(raw_path) if raw_path.exists() else list(coherent)
    rejected = read_rows(run_root / "stat_arb_pca_rejected.csv")
    return {
        "raw_rows": len(raw),
        "coherent_rows": len(coherent),
        "rejected_rows": len(rejected),
        "raw_positive": positive_count(raw, "raw_expected_edge"),
        "coherent_raw_positive": positive_count(coherent, "raw_expected_edge"),
        "rejected_raw_positive": positive_count(rejected, "raw_expected_edge"),
        "coherent_maker_positive": positive_count(coherent, "maker_entry_net_edge"),
        "top_raw": sorted(
            raw,
            key=lambda row: fnum(row.get("maker_entry_net_edge"), float("-inf")),
            reverse=True,
        )[:8],
        "top_rejected": sorted(
            rejected,
            key=lambda row: fnum(row.get("maker_entry_net_edge"), float("-inf")),
            reverse=True,
        )[:8],
    }


def intent_summary(path: Path) -> dict:
    rows = read_rows(path)
    bundles = {row.get("bundle_id", "") for row in rows if row.get("bundle_id")}
    strategies = Counter(row.get("strategy", "UNKNOWN") for row in rows)
    top = sorted(
        rows,
        key=lambda row: fnum(row.get("expected_edge"), float("-inf")),
        reverse=True,
    )[:12]
    return {
        "rows": len(rows),
        "bundles": len(bundles),
        "strategies": dict(sorted(strategies.items())),
        "max_expected_edge": max(
            (fnum(row.get("expected_edge")) for row in rows), default=0.0
        ),
        "top_legs": top,
    }


def shadow_fillability(run_root: Path, tape_window_seconds: int) -> dict:
    shadow = run_root / "shadow_b1"
    legs = read_rows(shadow / "multileg_legs.csv")
    bundles = read_rows(shadow / "multileg_bundles.csv")
    tape = read_rows(run_root / "trade_tape.csv")

    timestamps = [inum(row.get("timestamp")) for row in tape if inum(row.get("timestamp")) > 0]
    observed_span_seconds = max(timestamps) - min(timestamps) if len(timestamps) >= 2 else 0
    window_seconds = max(1, tape_window_seconds)
    sell_flow: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in tape:
        if (row.get("side") or "").upper() != "SELL":
            continue
        token = row.get("asset_id", "")
        price = fnum(row.get("price"), -1.0)
        size = max(0.0, fnum(row.get("size")))
        if token and price > 0.0 and size > 0.0:
            sell_flow[token].append((price, size))

    leg_out = []
    bundle_clear: dict[str, list[float | None]] = defaultdict(list)
    for leg in legs:
        token = leg.get("token_id", "")
        limit_price = fnum(leg.get("limit_price"))
        queue = max(0.0, fnum(leg.get("queue_ahead")))
        target = max(0.0, fnum(leg.get("target_shares")))
        compatible = sum(
            size
            for price, size in sell_flow.get(token, [])
            if price <= limit_price + 1e-12
        )
        rate = compatible / window_seconds
        clear_seconds = (queue + target) / rate if rate > 1e-12 else None
        queue_to_flow = queue / compatible if compatible > 1e-12 else None
        bundle_id = leg.get("bundle_id", "")
        bundle_clear[bundle_id].append(clear_seconds)
        leg_out.append(
            {
                "bundle_id": bundle_id,
                "market_id": leg.get("market_id", ""),
                "side": leg.get("side", ""),
                "token_id": token,
                "limit_price": limit_price,
                "queue_ahead_shares": queue,
                "target_shares": target,
                "compatible_sell_volume": compatible,
                "compatible_sell_rate_per_second": rate,
                "queue_to_recent_sell_volume": queue_to_flow,
                "estimated_queue_plus_target_clear_seconds": clear_seconds,
            }
        )

    bundle_out = []
    bundle_by_id = {row.get("bundle_id", ""): row for row in bundles}
    for bundle_id, values in sorted(bundle_clear.items()):
        finite = [value for value in values if value is not None]
        bundle = bundle_by_id.get(bundle_id, {})
        bundle_out.append(
            {
                "bundle_id": bundle_id,
                "strategy": bundle.get("strategy", ""),
                "expected_edge": fnum(bundle.get("expected_edge")),
                "all_legs_have_recent_compatible_flow": (
                    len(finite) == len(values) and bool(values)
                ),
                "max_estimated_clear_seconds": (
                    max(finite) if len(finite) == len(values) and finite else None
                ),
            }
        )

    return {
        "z_threshold": 1.25,
        "tape_window_seconds": window_seconds,
        "tape_observed_span_seconds": observed_span_seconds,
        "candidates": top_rows(shadow / "stat_arb_pairs.csv", "maker_entry_net_edge"),
        "intents": intent_summary(shadow / "intents.csv"),
        "legs": leg_out,
        "bundles": bundle_out,
        "log_tail": tail(shadow / "stat_arb_pairs_latest.log", 12),
    }


def trade_recorder_health(run_root: Path, now_ts: int, max_trade_age_seconds: int,
                          max_future_skew_seconds: int) -> dict[str, object]:
    path = run_root / "trade_recorder_latest.log"
    if not path.exists():
        return {
            "status": "not_evaluated",
            "failures": ["missing_trade_recorder_log"],
        }
    try:
        fields = parse_trade_recorder_status(path.read_text(encoding="utf-8", errors="replace"))
        return evaluate_trade_recorder_health(
            fields,
            now_ts,
            max_trade_age_seconds,
            max_future_skew_seconds,
        )
    except (OSError, ValueError) as exc:
        return {"status": "unhealthy", "failures": [str(exc)]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--tail-lines", type=int, default=12)
    parser.add_argument("--trade-lookback-seconds", type=int, default=900)
    parser.add_argument("--max-trade-age-seconds", type=int, default=1200)
    parser.add_argument("--max-future-skew-seconds", type=int, default=30)
    args = parser.parse_args()

    now_ts = int(time.time())
    recorder_health = trade_recorder_health(
        args.run_root,
        now_ts,
        max(1, args.max_trade_age_seconds),
        max(0, args.max_future_skew_seconds),
    )

    walk = {}
    walk_path = args.run_root / "walk_forward.json"
    if walk_path.exists():
        try:
            walk = json.loads(walk_path.read_text(encoding="utf-8"))
        except Exception:
            walk = {}

    snapshot = {
        "schema": "polymarket_public_live_smoke_v2",
        "generated_ts": now_ts,
        "git_sha": args.git_sha,
        "github_run_id": args.run_id,
        "run_root": args.run_root.name,
        "data_health": {"trade_recorder": recorder_health},
        "metrics": metric_snapshot(args.run_root / "metrics.prom"),
        "walk_forward": walk,
        "candidates": {
            "b1": top_rows(args.run_root / "stat_arb_pairs.csv", "maker_entry_net_edge"),
            "b2": top_rows(args.run_root / "stat_arb_pca.csv", "maker_entry_net_edge"),
            "b3_rewards": top_rows(
                args.run_root / "reward_opportunities.csv",
                "conservative_daily_score",
            ),
        },
        "b2_coherence": b2_coherence_summary(args.run_root),
        "intents": intent_summary(args.run_root / "intents.csv"),
        "shadow_b1": shadow_fillability(
            args.run_root, args.trade_lookback_seconds
        ),
        "logs": {
            name: tail(args.run_root / relative, args.tail_lines)
            for name, relative in LOGS.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(args.output)

    if recorder_health.get("status") == "unhealthy":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
