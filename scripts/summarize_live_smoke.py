from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import Counter, defaultdict
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


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


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


def inum(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def top_rows(path: Path, edge_key: str, limit: int = 8) -> list[dict[str, str]]:
    rows = read_rows(path)
    rows.sort(key=lambda r: fnum(r.get(edge_key), float("-inf")), reverse=True)
    return rows[:limit]


def intent_summary(path: Path) -> dict:
    rows = read_rows(path)
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


def shadow_fillability(run_root: Path) -> dict:
    shadow = run_root / "shadow_b1"
    legs = read_rows(shadow / "multileg_legs.csv")
    bundles = read_rows(shadow / "multileg_bundles.csv")
    tape = read_rows(run_root / "trade_tape.csv")

    timestamps = [inum(r.get("timestamp")) for r in tape if inum(r.get("timestamp")) > 0]
    window_seconds = max(1, max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0
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
        compatible = sum(size for price, size in sell_flow.get(token, []) if price <= limit_price + 1e-12)
        rate = compatible / window_seconds if window_seconds > 0 else 0.0
        clear_seconds = (queue + target) / rate if rate > 1e-12 else None
        queue_to_flow = queue / compatible if compatible > 1e-12 else None
        bid = leg.get("bundle_id", "")
        bundle_clear[bid].append(clear_seconds)
        leg_out.append({
            "bundle_id": bid,
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
        })

    bundle_out = []
    bundle_by_id = {r.get("bundle_id", ""): r for r in bundles}
    for bid, values in sorted(bundle_clear.items()):
        finite = [x for x in values if x is not None]
        bundle = bundle_by_id.get(bid, {})
        bundle_out.append({
            "bundle_id": bid,
            "strategy": bundle.get("strategy", ""),
            "expected_edge": fnum(bundle.get("expected_edge")),
            "all_legs_have_recent_compatible_flow": len(finite) == len(values) and bool(values),
            "max_estimated_clear_seconds": max(finite) if len(finite) == len(values) and finite else None,
        })

    return {
        "z_threshold": 1.25,
        "tape_window_seconds": window_seconds,
        "candidates": top_rows(shadow / "stat_arb_pairs.csv", "maker_entry_net_edge"),
        "intents": intent_summary(shadow / "intents.csv"),
        "legs": leg_out,
        "bundles": bundle_out,
        "log_tail": tail(shadow / "stat_arb_pairs_latest.log", 12),
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
        "shadow_b1": shadow_fillability(args.run_root),
        "logs": {name: tail(args.run_root / rel, args.tail_lines) for name, rel in LOGS.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
