#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _integer(status: dict[str, Any], key: str) -> int:
    value = status.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} is missing or non-numeric")
    if not math.isfinite(float(value)):
        raise ValueError(f"{key} is non-finite")
    return int(value)


def _number(status: dict[str, Any], key: str) -> float:
    value = status.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} is missing or non-numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{key} is non-finite")
    return value


def validate_status(
    status: dict[str, Any],
    errors_text: str,
    *,
    max_feed_stale_ms: float,
    min_rest_resyncs: int,
) -> list[str]:
    failures: list[str] = []

    if status.get("mode") != "shadow":
        failures.append("fast market-data runtime is not in shadow mode")
    if status.get("real_order_submission") is not False:
        failures.append("fast market-data runtime is not read-only")

    try:
        workers = _integer(status, "ws_workers")
        if workers <= 0:
            failures.append("no WebSocket workers were configured")
    except ValueError as error:
        failures.append(str(error))

    try:
        messages = _integer(status, "ws_messages")
        if messages <= 0:
            failures.append("public WebSocket produced no market messages")
    except ValueError as error:
        failures.append(str(error))

    try:
        updates = _integer(status, "book_updates")
        if updates <= 0:
            failures.append("WebSocket messages produced no recognized book updates")
    except ValueError as error:
        failures.append(str(error))

    try:
        stale_ms = _number(status, "feed_stale_ms")
        if stale_ms < 0:
            failures.append("WebSocket freshness timestamp is unavailable")
        elif stale_ms > max_feed_stale_ms:
            failures.append(
                f"WebSocket market data is stale: {stale_ms:.0f} ms > {max_feed_stale_ms:.0f} ms"
            )
    except ValueError as error:
        failures.append(str(error))

    try:
        resyncs = _integer(status, "rest_resyncs")
        if resyncs < min_rest_resyncs:
            failures.append(
                f"insufficient successful CLOB REST book refreshes: {resyncs} < {min_rest_resyncs}"
            )
    except ValueError as error:
        failures.append(str(error))

    lowered = errors_text.lower()
    if "unsupported protocol" in lowered:
        failures.append("WebSocket transport reported an unsupported protocol")
    if "http 429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        failures.append("public market-data path hit an API rate limit")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed when the read-only fast market-data probe is stale or degraded"
    )
    parser.add_argument("--status", required=True)
    parser.add_argument("--errors", required=True)
    parser.add_argument("--max-feed-stale-ms", type=float, default=45000.0)
    parser.add_argument("--min-rest-resyncs", type=int, default=2)
    args = parser.parse_args()

    status = json.loads(Path(args.status).read_text(encoding="utf-8"))
    errors_text = Path(args.errors).read_text(encoding="utf-8")
    failures = validate_status(
        status,
        errors_text,
        max_feed_stale_ms=max(0.0, args.max_feed_stale_ms),
        min_rest_resyncs=max(0, args.min_rest_resyncs),
    )
    if failures:
        print("public market-data health check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(
        "public market-data health check passed: "
        f"ws_messages={status['ws_messages']} "
        f"book_updates={status['book_updates']} "
        f"feed_stale_ms={float(status['feed_stale_ms']):.0f} "
        f"rest_resyncs={status['rest_resyncs']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
