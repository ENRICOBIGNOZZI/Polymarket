from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_repricing_reason_audit as m


def row(reason, direction, delta, market="m1"):
    return {
        "schema": "polymarket_v7_native_repricing_label_v1",
        "origin_reason": reason,
        "signal_direction": direction,
        "repricing_horizon_ms": 250,
        "asset": "BTC",
        "horizon": "M5",
        "market_id": market,
        "delta_probability": delta,
    }


def test_reports_shock_aligned_residual_repricing_by_reason():
    report = m.summarize([
        row(14, 1, .02, "m1"),
        row(14, -1, -.01, "m2"),
        row(1, 1, -.01, "m3"),
    ])
    rejected = report["groups"]["14|BTC:M5|250"]
    assert rejected["n"] == 2
    assert rejected["markets"] == 2
    assert rejected["mean_shock_aligned_delta_probability"] == .015
    assert rejected["positive_fraction"] == 1.0
    assert report["midpoint_not_executable_pnl"] is True


def test_invalid_rows_are_counted_not_zero_imputed():
    report = m.summarize([{"schema": "wrong"}])
    assert report["groups"] == {}
    assert report["invalid_rows"] == 1
