#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import struct
from collections import defaultdict
from pathlib import Path

RECORD = struct.Struct("<QQQQqB7x")
STAGES = {
    1: "frame_receive",
    2: "decode_done",
    3: "arb_decision",
    4: "risk_admitted",
    5: "sign_start",
    6: "sign_done",
    7: "wire_start",
    8: "wire_complete",
    9: "http_ack",
    10: "user_ws_match",
}
SEGMENTS = (
    ("frame_receive", "decode_done"),
    ("decode_done", "arb_decision"),
    ("arb_decision", "risk_admitted"),
    ("risk_admitted", "sign_start"),
    ("sign_start", "sign_done"),
    ("sign_done", "wire_start"),
    ("wire_start", "wire_complete"),
    ("wire_complete", "http_ack"),
    ("http_ack", "user_ws_match"),
    ("frame_receive", "http_ack"),
    ("frame_receive", "user_ws_match"),
)


def quantile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    index = int(round(max(0.0, min(1.0, p)) * (len(values) - 1)))
    return values[index]


def dist(values: list[int]) -> dict[str, int | None]:
    return {
        "count": len(values),
        "p50": quantile(values, 0.50),
        "p95": quantile(values, 0.95),
        "p99": quantile(values, 0.99),
        "p999": quantile(values, 0.999),
        "max": quantile(values, 1.0),
    }


def summarize(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) % RECORD.size:
        raise ValueError("latency tape has a truncated record")
    traces: dict[int, dict[str, int]] = defaultdict(dict)
    duplicate_stage = 0
    invalid_stage = 0
    for offset in range(0, len(raw), RECORD.size):
        trace_id, _client, _market, _instrument, timestamp_ns, stage = RECORD.unpack_from(raw, offset)
        name = STAGES.get(stage)
        if not trace_id or timestamp_ns <= 0 or name is None:
            invalid_stage += 1
            continue
        if name in traces[trace_id]:
            duplicate_stage += 1
            traces[trace_id][name] = min(traces[trace_id][name], timestamp_ns)
        else:
            traces[trace_id][name] = timestamp_ns

    samples: dict[str, list[int]] = {
        f"{start}_to_{end}": [] for start, end in SEGMENTS
    }
    non_monotone = 0
    complete_http = 0
    complete_user_ws = 0
    for trace in traces.values():
        if "frame_receive" in trace and "http_ack" in trace:
            complete_http += 1
        if "frame_receive" in trace and "user_ws_match" in trace:
            complete_user_ws += 1
        for start, end in SEGMENTS:
            if start not in trace or end not in trace:
                continue
            delta = trace[end] - trace[start]
            if delta < 0:
                non_monotone += 1
                continue
            samples[f"{start}_to_{end}"].append(delta)

    return {
        "schema": "polymarket_v7_native_latency_summary_v1",
        "record_size": RECORD.size,
        "records": len(raw) // RECORD.size,
        "traces": len(traces),
        "complete_http_traces": complete_http,
        "complete_user_ws_traces": complete_user_ws,
        "duplicate_stage_records": duplicate_stage,
        "invalid_stage_records": invalid_stage,
        "non_monotone_segments": non_monotone,
        "latency_ns": {key: dist(value) for key, value in samples.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tape", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = summarize(args.tape)
    payload = json.dumps(result, sort_keys=True)
    if args.output is not None:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
