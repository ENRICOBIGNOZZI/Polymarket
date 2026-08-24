#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

RECORDER_LINE = re.compile(r"^trade_recorder\s+(?P<fields>.+)$")
REQUIRED_FIELDS = (
    "markets",
    "conditions",
    "requests",
    "fetched",
    "new_trades",
    "errors",
    "truncated_batches",
    "last_trade_ts",
    "seen",
    "elapsed_ms",
)


def parse_status_line(text: str) -> dict[str, int]:
    line = ""
    for candidate in reversed(text.splitlines()):
        if candidate.startswith("trade_recorder "):
            line = candidate.strip()
            break
    if not line:
        raise ValueError("missing trade_recorder status line")

    match = RECORDER_LINE.match(line)
    if not match:
        raise ValueError("malformed trade_recorder status line")

    fields: dict[str, int] = {}
    for token in match.group("fields").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        try:
            fields[key] = int(value)
        except ValueError as exc:
            raise ValueError(f"non-integer {key}={value}") from exc

    missing = [key for key in REQUIRED_FIELDS if key not in fields]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    return fields


def evaluate(fields: dict[str, int], now_ts: int, max_trade_age_seconds: int,
             max_future_skew_seconds: int) -> dict[str, object]:
    failures: list[str] = []

    if fields["markets"] <= 0:
        failures.append("no_markets_discovered")
    if fields["conditions"] <= 0:
        failures.append("no_condition_ids")
    if fields["markets"] > 0 and fields["conditions"] != fields["markets"]:
        failures.append("condition_mapping_incomplete")
    if fields["requests"] <= 0:
        failures.append("no_data_api_requests")
    if fields["errors"] > 0:
        failures.append("data_api_request_errors")
    if fields["truncated_batches"] > 0:
        failures.append("trade_batches_truncated")
    if fields["fetched"] <= 0:
        failures.append("no_public_trades_fetched")
    if fields["last_trade_ts"] <= 0:
        failures.append("missing_last_trade_timestamp")

    trade_age_seconds: int | None = None
    if fields["last_trade_ts"] > 0:
        trade_age_seconds = now_ts - fields["last_trade_ts"]
        if trade_age_seconds < -max_future_skew_seconds:
            failures.append("trade_timestamp_in_future")
        elif trade_age_seconds > max_trade_age_seconds:
            failures.append("stale_public_trade_tape")

    return {
        "status": "healthy" if not failures else "unhealthy",
        "failures": failures,
        "trade_age_seconds": trade_age_seconds,
        "max_trade_age_seconds": max_trade_age_seconds,
        "max_future_skew_seconds": max_future_skew_seconds,
        "fields": fields,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail closed on degraded public trade-recorder evidence")
    parser.add_argument("--log", required=True)
    parser.add_argument("--max-trade-age-seconds", type=int, default=1200)
    parser.add_argument("--max-future-skew-seconds", type=int, default=30)
    parser.add_argument("--now-ts", type=int)
    parser.add_argument("--output")
    args = parser.parse_args()

    try:
        fields = parse_status_line(Path(args.log).read_text(encoding="utf-8", errors="replace"))
        report = evaluate(
            fields,
            int(time.time()) if args.now_ts is None else args.now_ts,
            max(1, args.max_trade_age_seconds),
            max(0, args.max_future_skew_seconds),
        )
    except (OSError, ValueError) as exc:
        report = {"status": "unhealthy", "failures": [str(exc)]}

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report.get("status") == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
