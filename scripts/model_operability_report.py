#!/usr/bin/env python3
"""Publish an execution-aware status for every V5 alpha model.

The report distinguishes a dead/stale process from valid abstention, missing
input, a non-executable research model, passive orders waiting in queue, and an
actually active book. It is diagnostic only and never relaxes an edge gate.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _now() -> int:
    return int(time.time())


def _float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_csv(path: Path, header_prefix: str | None = None) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if header_prefix:
        start = next((i for i, line in enumerate(lines) if line.startswith(header_prefix)), None)
        if start is None:
            return []
        lines = lines[start:]
    if not lines:
        return []
    try:
        return list(csv.DictReader(lines))
    except (csv.Error, TypeError):
        return []


def _file_age(path: Path, now: int) -> float:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return 1e12


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_csv(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    fields = (
        "name", "model", "backend", "entry_enabled", "state", "reason", "process_alive",
        "status_age_seconds", "signals", "gross_positive", "cost_positive", "net_positive",
        "orders", "fills", "positions", "best_net_edge",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})
    os.replace(temporary, path)


def _recent(rows: Iterable[dict[str, str]], now: int, window: int, *keys: str) -> list[dict[str, str]]:
    cutoff = now - max(1, window)
    out: list[dict[str, str]] = []
    for row in rows:
        ts = max((_int(row.get(key)) for key in keys), default=0)
        if ts >= cutoff:
            out.append(row)
    return out


def _positive(rows: Iterable[dict[str, str]], key: str, threshold: float = 0.0) -> int:
    return sum(_float(row.get(key), float("-inf")) > threshold for row in rows)


def _best(rows: Iterable[dict[str, str]], key: str) -> float:
    return max((_float(row.get(key), float("-inf")) for row in rows), default=0.0)


def _count_rows(path: Path) -> int:
    return len(_read_csv(path))


def _routing(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    multi = config.get("multi_strategy") if isinstance(config.get("multi_strategy"), dict) else {}
    operability = multi.get("operability") if isinstance(multi.get("operability"), dict) else {}
    configured = operability.get("execution_routing")
    if isinstance(configured, dict):
        return {
            str(name): dict(value)
            for name, value in configured.items()
            if isinstance(value, dict)
        }
    return {
        "micro": {"backend": "maker_paper", "entry_enabled": True},
        "pca": {"backend": "multileg_b2", "entry_enabled": True},
        "graph": {"backend": "negrisk_basket_scan", "entry_enabled": False},
        "semantic": {"backend": "shadow_only", "entry_enabled": False},
        "external": {"backend": "shadow_only", "entry_enabled": False},
        "stat_arb_pairs": {"backend": "multileg_b1", "entry_enabled": True},
    }


def _generic_metrics(run_root: Path, name: str, now: int, window: int) -> dict[str, Any]:
    rows = _recent(_read_csv(run_root / "strategies" / name / "signals.csv"), now, window, "timestamp")
    return {
        "signals": len(rows),
        "gross_positive": _positive(rows, "gross_edge"),
        "cost_positive": _positive(rows, "cost_adjusted_edge"),
        "net_positive": _positive(rows, "net_edge"),
        "best_net_edge": _best(rows, "net_edge"),
    }


def _base_row(name: str, model: str, route: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "model": model,
        "backend": str(route.get("backend", "unknown")),
        "entry_enabled": bool(route.get("entry_enabled", False)),
        "state": "UNKNOWN",
        "reason": str(route.get("reason", "")),
        "process_alive": False,
        "status_age_seconds": 1e12,
        "signals": 0,
        "gross_positive": 0,
        "cost_positive": 0,
        "net_positive": 0,
        "orders": 0,
        "fills": 0,
        "positions": 0,
        "best_net_edge": 0.0,
    }


def build_report(
    config_path: Path,
    run_root: Path,
    *,
    now: int | None = None,
    window_seconds: int = 3600,
    stale_seconds: float = 600.0,
) -> dict[str, Any]:
    current = _now() if now is None else int(now)
    config = _read_json(config_path)
    routing = _routing(config)
    strategy_status = {row.get("name", ""): row for row in _read_csv(run_root / "strategy_status.csv")}
    rows: list[dict[str, Any]] = []

    # Micro: the generic one-expert child is shadow-only; passive maker is the
    # executable short-horizon route.
    route = routing.get("micro", {})
    row = _base_row("micro", "microstructure", route)
    row.update(_generic_metrics(run_root, "micro", current, window_seconds))
    status = strategy_status.get("micro", {})
    row["process_alive"] = _int(status.get("alive")) == 1
    row["status_age_seconds"] = _float(status.get("status_age_seconds"), 1e12)
    maker_orders = _recent(_read_csv(run_root / "maker" / "maker_order_log.csv"), current, window_seconds, "timestamp")
    maker_fills = _recent(_read_csv(run_root / "maker" / "maker_fills.csv"), current, window_seconds, "timestamp")
    row["orders"] = sum((entry.get("action") or "").upper() == "POST" for entry in maker_orders)
    row["fills"] = len(maker_fills)
    row["positions"] = _count_rows(run_root / "maker" / "maker_positions.csv")
    maker_age = min(
        _file_age(run_root / "maker" / "maker_equity.csv", current),
        _file_age(run_root / "maker.log", current),
    )
    if maker_age > stale_seconds:
        row.update(state="STALE", reason="maker backend is not publishing fresh state")
    elif row["positions"] or row["fills"]:
        row.update(state="ACTIVE", reason="passive maker has evidenced fills or open positions")
    elif row["orders"]:
        row.update(state="QUOTING_NO_FILL", reason="passive quotes are resting; no evidenced queue fill")
    else:
        row.update(state="READY_NO_QUOTE", reason="maker is fresh but no quote passed costs and adverse-selection gates")
    rows.append(row)

    # B2 PCA multi-leg route.
    route = routing.get("pca", {})
    row = _base_row("pca", "pca_stat_arb", route)
    row.update(_generic_metrics(run_root, "pca", current, window_seconds))
    status = strategy_status.get("pca", {})
    row["process_alive"] = _int(status.get("alive")) == 1
    row["status_age_seconds"] = _float(status.get("status_age_seconds"), 1e12)
    b2 = _read_csv(run_root / "stat_arb_pca.csv")
    b2_intents = [r for r in _read_csv(run_root / "intents.csv") if (r.get("strategy") or "").upper() == "B2"]
    bundles = [r for r in _read_csv(run_root / "multileg_bundles.csv") if (r.get("strategy") or "").upper() == "B2"]
    row["signals"] = len(b2)
    row["gross_positive"] = _positive(b2, "raw_expected_edge")
    row["cost_positive"] = _positive(b2, "maker_entry_net_edge")
    row["net_positive"] = _positive(b2, "maker_entry_net_edge")
    row["best_net_edge"] = _best(b2, "maker_entry_net_edge")
    row["orders"] = len(b2_intents)
    row["positions"] = len(bundles)
    if bundles:
        row.update(state="ACTIVE", reason="B2 bundles are admitted in the multi-leg broker")
    elif b2_intents:
        row.update(state="ADMITTED_WAITING", reason="B2 intents exist and await broker/fill evidence")
    elif b2 and row["cost_positive"] == 0:
        row.update(state="ABSTAIN_NEGATIVE_POST_COST_EDGE", reason="PCA dislocations do not clear executable maker costs")
    elif b2:
        row.update(state="READY_NO_INTENT", reason="positive rows exist but failed coherence, freshness, or risk admission")
    else:
        row.update(state="NO_CANDIDATES", reason="no coherent PCA hedge candidate in the latest scan")
    rows.append(row)

    # B1 pair stat-arb route.
    route = routing.get("stat_arb_pairs", {"backend": "multileg_b1", "entry_enabled": True})
    row = _base_row("stat_arb_pairs", "pair_stat_arb", route)
    b1 = _read_csv(run_root / "stat_arb_pairs.csv")
    b1_intents = [r for r in _read_csv(run_root / "intents.csv") if (r.get("strategy") or "").upper() == "B1"]
    bundles = [r for r in _read_csv(run_root / "multileg_bundles.csv") if (r.get("strategy") or "").upper() == "B1"]
    row["process_alive"] = _int((_read_csv(run_root / "runtime_supervisor.csv") or [{}])[-1].get("broker_alive")) == 1
    row["status_age_seconds"] = _file_age(run_root / "stat_arb_pairs.csv", current)
    row["signals"] = len(b1)
    row["gross_positive"] = _positive(b1, "raw_expected_edge")
    row["cost_positive"] = _positive(b1, "maker_entry_net_edge")
    row["net_positive"] = _positive(b1, "maker_entry_net_edge")
    row["best_net_edge"] = _best(b1, "maker_entry_net_edge")
    row["orders"] = len(b1_intents)
    row["positions"] = len(bundles)
    if row["status_age_seconds"] > stale_seconds:
        row.update(state="STALE", reason="B1 scanner output is stale or missing")
    elif bundles:
        row.update(state="ACTIVE", reason="B1 bundles are admitted in the multi-leg broker")
    elif b1_intents:
        row.update(state="ADMITTED_WAITING", reason="B1 intents exist and await broker/fill evidence")
    elif b1 and row["cost_positive"] == 0:
        row.update(state="ABSTAIN_NEGATIVE_POST_COST_EDGE", reason="pair dislocations do not clear executable maker costs")
    else:
        row.update(state="NO_CANDIDATES", reason="no production-admissible B1 pair")
    rows.append(row)

    # Graph constraints: only complete baskets are identified; single-leg generic
    # entries remain disabled.
    route = routing.get("graph", {})
    row = _base_row("graph", "event_graph", route)
    row.update(_generic_metrics(run_root, "graph", current, window_seconds))
    status = strategy_status.get("graph", {})
    row["process_alive"] = _int(status.get("alive")) == 1
    row["status_age_seconds"] = _float(status.get("status_age_seconds"), 1e12)
    structural = _read_csv(run_root / "structural_latest.csv", "type,event_id,")
    row["signals"] = len(structural)
    row["gross_positive"] = _positive(structural, "raw_edge")
    row["cost_positive"] = sum(
        _float(r.get("net_edge_pre_gas"), float("-inf")) > 0.0
        and _float(r.get("executable_shares")) > 0.0
        for r in structural
    )
    row["net_positive"] = row["cost_positive"]
    row["best_net_edge"] = _best(structural, "net_edge_pre_gas")
    if bool(route.get("entry_enabled", False)):
        row.update(state="MISCONFIGURED", reason="graph single-leg entry must remain disabled; execute complete baskets only")
    elif row["net_positive"]:
        row.update(state="SHADOW_POSITIVE_NO_BASKET_BROKER", reason="complete-set diagnostic is positive but no leg-level atomic broker is approved")
    else:
        row.update(state="SHADOW_NO_POST_COST_EDGE", reason="no executable complete event basket is positive after costs")
    rows.append(row)

    # Semantic relative value: generic token overlap is not a terminal-probability
    # model and has known negation/threshold counterexamples.
    route = routing.get("semantic", {})
    row = _base_row("semantic", "semantic_relative_value", route)
    row.update(_generic_metrics(run_root, "semantic", current, window_seconds))
    status = strategy_status.get("semantic", {})
    row["process_alive"] = _int(status.get("alive")) == 1
    row["status_age_seconds"] = _float(status.get("status_age_seconds"), 1e12)
    if bool(route.get("entry_enabled", False)):
        row.update(state="MISCONFIGURED", reason="semantic entry enabled without relation/polarity identification")
    else:
        row.update(state="SHADOW_UNIDENTIFIED_RELATION", reason="negation, threshold, horizon, and event relation are not identified")
    rows.append(row)

    # External terminal probabilities need a fresh approved feed.
    route = routing.get("external", {})
    row = _base_row("external", "external_information", route)
    row.update(_generic_metrics(run_root, "external", current, window_seconds))
    status = strategy_status.get("external", {})
    row["process_alive"] = _int(status.get("alive")) == 1
    row["status_age_seconds"] = _float(status.get("status_age_seconds"), 1e12)
    external_rows = _read_csv(Path(str(config.get("external_signals_file", "data/external_signals.csv"))))
    fresh_external = [
        r for r in external_rows
        if 0 < _int(r.get("timestamp")) >= current - window_seconds and _float(r.get("confidence")) > 0.0
    ]
    row["signals"] = len(fresh_external)
    if bool(route.get("entry_enabled", False)):
        row.update(state="MISCONFIGURED", reason="external entry enabled before a fresh approved feed exists")
    elif not fresh_external:
        row.update(state="BLOCKED_NO_FRESH_APPROVED_FEED", reason="configured external CSV has no fresh positive-confidence rows")
    else:
        row.update(state="SHADOW_FEED_PRESENT_NOT_PROMOTED", reason="fresh rows exist but the feed has not passed promotion evidence")
    rows.append(row)

    stale_or_dead = [
        r["name"] for r in rows
        if r["state"] == "STALE" or (r["name"] in strategy_status and not r["process_alive"])
    ]
    active = [r["name"] for r in rows if r["state"] == "ACTIVE"]
    ready_waiting = [r["name"] for r in rows if r["state"] in {"QUOTING_NO_FILL", "ADMITTED_WAITING", "READY_NO_QUOTE", "READY_NO_INTENT"}]
    abstaining = [r["name"] for r in rows if r["state"].startswith("ABSTAIN") or r["state"] == "NO_CANDIDATES"]
    shadow = [r["name"] for r in rows if r["state"].startswith("SHADOW") or r["state"].startswith("BLOCKED")]

    return {
        "schema": "polymarket_model_operability_v1",
        "timestamp": current,
        "window_seconds": window_seconds,
        "stale_seconds": stale_seconds,
        "generic_children_scan_only": bool(
            ((config.get("multi_strategy") or {}).get("operability") or {}).get("generic_children_scan_only", False)
        ),
        "summary": {
            "models": len(rows),
            "active": active,
            "ready_waiting": ready_waiting,
            "valid_abstention": abstaining,
            "shadow_or_blocked": shadow,
            "stale_or_dead": stale_or_dead,
            "state_counts": dict(Counter(str(r["state"]) for r in rows)),
        },
        "models": rows,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/paper_v5.json"))
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v5_live"))
    parser.add_argument("--window-seconds", type=int, default=3600)
    parser.add_argument("--stale-seconds", type=float, default=600.0)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--loop", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        try:
            report = build_report(
                args.config,
                args.run_root,
                window_seconds=args.window_seconds,
                stale_seconds=args.stale_seconds,
            )
            _atomic_json(args.run_root / "model_operability.json", report)
            _atomic_csv(args.run_root / "model_operability.csv", report["models"])
        except Exception as exc:  # keep reporting failures visible without killing trading
            _atomic_json(
                args.run_root / "model_operability.json",
                {
                    "schema": "polymarket_model_operability_v1",
                    "timestamp": _now(),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        if not args.loop:
            return 0
        time.sleep(max(1.0, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
