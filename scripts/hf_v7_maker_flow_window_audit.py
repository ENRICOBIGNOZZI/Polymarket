#!/usr/bin/env python3
"""Audit whether V7 maker admission uses the configured causal rolling tape window.

This is research-only evidence.  Fill replay must remain watermark/incremental so
public prints are consumed once; admission/fill-hazard estimation must instead
see all causally known event-time prints inside the configured lookback.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

BLOCKING = "BLOCKING_ROLLING_FLOW_WINDOW_TRUNCATED"
OK = "ROLLING_FLOW_WINDOW_SEPARATED_FROM_FILL_REPLAY"


def audit_source(source: str, config: dict[str, Any]) -> dict[str, Any]:
    lookback = int(config.get("flow_lookback_seconds") or 0)
    interval_match = re.search(r'parser\.add_argument\("--interval"[^\n]*default\s*=\s*([0-9.]+)', source)
    interval = float(interval_match.group(1)) if interval_match else None

    incremental_read = "new_trades = read_new_tape(trade_tape, state)" in source
    watermark_advanced = "advance_tape_watermark(state, new_trades)" in source
    decision_from_incremental = bool(re.search(
        r"recent_for_decision\s*=\s*\[\s*t\s+for\s+t\s+in\s+new_trades\b",
        source,
    ))
    dedicated_rolling_reader = bool(re.search(
        r"recent_for_decision\s*=\s*read_[a-zA-Z0-9_]*recent[a-zA-Z0-9_]*tape\(",
        source,
    ))

    blocking = incremental_read and watermark_advanced and decision_from_incremental and not dedicated_rolling_reader
    nominal_fraction = None
    if blocking and interval is not None and lookback > 0:
        nominal_fraction = min(1.0, interval / float(lookback))

    return {
        "schema": "polymarket_hf_v7_maker_flow_window_audit_v1",
        "status": BLOCKING if blocking else OK,
        "blocking": blocking,
        "configured_flow_lookback_seconds": lookback,
        "default_worker_interval_seconds": interval,
        "nominal_interval_to_lookback_fraction": nominal_fraction,
        "incremental_fill_replay_present": incremental_read,
        "watermark_advance_present": watermark_advanced,
        "decision_flow_uses_only_incremental_rows": decision_from_incremental,
        "dedicated_causal_rolling_decision_reader_present": dedicated_rolling_reader,
        "required_contract": {
            "fill_replay": "watermark/incremental; consume each public print at most once",
            "decision_flow": "independent rolling event-time window; received_ms<=decision_ms; event_ts_ms<=decision_ms; event age<=configured lookback",
            "no_future_receive_leakage": True,
            "no_double_fill_replay": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("scripts/v7_complete_set_maker.py"))
    parser.add_argument("--config", type=Path, default=Path("config/v7_complete_set_maker.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.source.read_text(encoding="utf-8")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = audit_source(source, config)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 2 if report["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
