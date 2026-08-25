#!/usr/bin/env python3
"""Convert pure B1/B2 scanner CSVs into standardized V4 broker intents.

The scanners remain alpha-only. This adapter owns the execution-facing schema.
B2 additionally requires explicit coherence evidence from
``filter_coherent_hedges.py``; a raw PCA row can never reach the broker.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
from pathlib import Path

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


def fnum(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        out = float(row.get(key, default) or default)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def load_config(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def deadlines(now: int, half_life_hours: float) -> tuple[int, int]:
    execution = now + 300
    hold_seconds = int(min(86400.0, max(300.0, max(0.0, half_life_hours) * 3600.0)))
    return execution, execution + hold_seconds


def b1_rows(rows: list[dict[str, str]], now: int, min_edge: float, max_trade: float):
    out: list[dict[str, object]] = []
    serial = 0
    for r in rows:
        edge = fnum(r, "maker_entry_net_edge")
        executable = max(0.0, fnum(r, "executable_notional"))
        y, x = (r.get("y_market") or "").strip(), (r.get("x_market") or "").strip()
        ys, xs = (r.get("y_side") or "").strip(), (r.get("x_side") or "").strip()
        yw, xw = fnum(r, "y_weight"), fnum(r, "x_weight")
        yl, xl = fnum(r, "y_limit"), fnum(r, "x_limit")
        if edge <= min_edge or executable <= 0 or not y or not x or y == x:
            continue
        if ys not in {"YES", "NO"} or xs not in {"YES", "NO"} or yw <= 0 or xw <= 0:
            continue
        cap = min(max_trade, executable)
        if cap <= 0:
            continue
        ex, hold = deadlines(now, fnum(r, "half_life_h"))
        bundle = f"B1-{now}-{y}-{x}-{serial}"
        serial += 1
        common = dict(
            bundle_id=bundle,
            strategy="B1",
            event_id=f"PAIR:{y}|{x}",
            created_ts=now,
            mode="MAKER",
            expected_edge=edge,
            max_notional=cap,
            execution_deadline_ts=ex,
            hold_deadline_ts=hold,
        )
        out.append({**common, "market_id": y, "side": ys, "weight": yw, "limit_price": yl})
        out.append({**common, "market_id": x, "side": xs, "weight": xw, "limit_price": xl})
    return out


def parse_pca_legs(raw: str):
    legs = []
    for item in (raw or "").split("|"):
        parts = item.split(":")
        if len(parts) != 3:
            return []
        market, side, weight_s = parts
        try:
            weight = float(weight_s)
        except ValueError:
            return []
        if not market or side not in {"YES", "NO"} or not math.isfinite(weight) or weight <= 0:
            return []
        legs.append((market, side, weight))
    return legs


def b2_coherence_valid(row: dict[str, str]) -> bool:
    """Require the same positive relation certificate emitted by the B2 coherence gate.

    ``filter_coherent_hedges.py`` admits hedge legs related by same event,
    same non-empty Polymarket category, or semantic overlap. The execution
    adapter must recognize exactly those positive certificate families; an
    unknown or unrelated certificate remains fail-closed.
    """
    raw = (row.get("coherence_scope") or "").strip()
    if not raw:
        return False
    scopes = [part.strip() for part in raw.split("|") if part.strip()]
    return bool(scopes) and all(
        scope in {"same_event", "same_category", "semantic"}
        or scope.startswith("same_event:")
        or scope.startswith("same_category:")
        or scope.startswith("semantic:")
        for scope in scopes
    )


def b2_rows(rows: list[dict[str, str]], now: int, min_edge: float, max_trade: float):
    out: list[dict[str, object]] = []
    serial = 0
    for r in rows:
        if not b2_coherence_valid(r):
            continue
        edge = fnum(r, "maker_entry_net_edge")
        executable = max(0.0, fnum(r, "executable_notional"))
        target = (r.get("market") or "").strip()
        legs = parse_pca_legs(r.get("legs") or "")
        if edge <= min_edge or executable <= 0 or not target or len(legs) < 2:
            continue
        if len({(m, s) for m, s, _ in legs}) != len(legs):
            continue
        if target not in {market for market, _, _ in legs}:
            continue
        cap = min(max_trade, executable)
        if cap <= 0:
            continue
        ex, hold = deadlines(now, fnum(r, "half_life_h"))
        bundle = f"B2-{now}-{target}-{serial}"
        serial += 1
        common = dict(
            bundle_id=bundle,
            strategy="B2",
            event_id=f"PCA:{target}",
            created_ts=now,
            mode="MAKER",
            expected_edge=edge,
            max_notional=cap,
            execution_deadline_ts=ex,
            hold_deadline_ts=hold,
        )
        for market, side, weight in legs:
            # Zero means the broker resolves the current passive best bid at admission.
            out.append(
                {
                    **common,
                    "market_id": market,
                    "side": side,
                    "weight": weight,
                    "limit_price": 0.0,
                }
            )
    return out


def atomic_write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["B1", "B2"], required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/paper_v4.json"))
    parser.add_argument("--min-edge", type=float, default=0.001)
    parser.add_argument("--max-trade-usd", type=float, default=None)
    parser.add_argument("--now", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    max_trade = (
        args.max_trade_usd
        if args.max_trade_usd is not None
        else fnum(cfg, "max_trade_usd", 250.0)
    )
    max_trade = max(0.0, max_trade)
    now = int(time.time()) if args.now is None else args.now
    try:
        with args.input.open(newline="", encoding="utf-8") as f:
            source = list(csv.DictReader(f))
    except FileNotFoundError:
        source = []

    if args.strategy == "B1":
        rows = b1_rows(source, now, args.min_edge, max_trade)
        coherence_rejected = 0
    else:
        coherence_rejected = sum(not b2_coherence_valid(row) for row in source)
        rows = b2_rows(source, now, args.min_edge, max_trade)
    atomic_write(args.output, rows)
    print(
        json.dumps(
            {
                "strategy": args.strategy,
                "scanner_rows": len(source),
                "coherence_rejected": coherence_rejected,
                "intent_rows": len(rows),
                "bundles": len({row["bundle_id"] for row in rows}),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
