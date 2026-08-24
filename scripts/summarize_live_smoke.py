from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter
from pathlib import Path

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
    "b1": "stat_arb_pairs_latest.log",
    "b2": "stat_arb_pca_latest.log",
    "multileg": "multileg_latest.log",
    "maker": "maker_latest.log",
    "terminal": "terminal_latest.log",
}


def metric_snapshot(path: Path) -> dict:
    out: dict[str, float | dict] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = METRIC.match(raw.strip())
        if not m:
            continue
        name, labels, value = m.groups()
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


def top_rows(path: Path, edge_key: str, limit: int = 8) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    rows.sort(key=lambda r: fnum(r.get(edge_key), float("-inf")), reverse=True)
    return rows[:limit]


def intent_summary(path: Path) -> dict:
    if not path.exists():
        return {"rows": 0, "bundles": 0, "strategies": {}, "max_expected_edge": 0.0, "top_legs": []}
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        rows = []
    bundles = {r.get("bundle_id", "") for r in rows if r.get("bundle_id")}
    strategies = Counter(r.get("strategy", "UNKNOWN") for r in rows)
    top = sorted(rows, key=lambda r: fnum(r.get("expected_edge"), float("-inf")), reverse=True)[:12]
    return {
        "rows": len(rows),
        "bundles": len(bundles),
        "strategies": dict(sorted(strategies.items())),
        "max_expected_edge": max((fnum(r.get("expected_edge")) for r in rows), default=0.0),
        "top_legs": top,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-root", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--git-sha", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--tail-lines", type=int, default=12)
    args = ap.parse_args()

    walk = {}
    walk_path = args.run_root / "walk_forward.json"
    if walk_path.exists():
        try:
            walk = json.loads(walk_path.read_text(encoding="utf-8"))
        except Exception:
            walk = {}

    snapshot = {
        "schema": "polymarket_public_live_smoke_v1",
        "generated_ts": int(time.time()),
        "git_sha": args.git_sha,
        "github_run_id": args.run_id,
        "run_root": args.run_root.name,
        "metrics": metric_snapshot(args.run_root / "metrics.prom"),
        "walk_forward": walk,
        "candidates": {
            "b1": top_rows(args.run_root / "stat_arb_pairs.csv", "maker_entry_net_edge"),
            "b2": top_rows(args.run_root / "stat_arb_pca.csv", "maker_entry_net_edge"),
        },
        "intents": intent_summary(args.run_root / "intents.csv"),
        "logs": {name: tail(args.run_root / rel, args.tail_lines) for name, rel in LOGS.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
