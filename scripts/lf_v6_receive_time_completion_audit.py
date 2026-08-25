#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradeClock:
    event_ts_s: int
    received_ms: int


def active(clock_ms: int, arrival_ms: int, cancel_effective_ms: int) -> bool:
    return arrival_ms <= clock_ms < cancel_effective_ms


def event_time_active(trade: TradeClock, arrival_ms: int, cancel_effective_ms: int) -> bool:
    return active(trade.event_ts_s * 1000, arrival_ms, cancel_effective_ms)


def receive_time_active(trade: TradeClock, arrival_ms: int, cancel_effective_ms: int) -> bool:
    return active(trade.received_ms, arrival_ms, cancel_effective_ms)


def consume(queue_ahead: float, remaining: float, compatible_size: float, eligible: bool) -> tuple[float, float]:
    if not eligible or compatible_size <= 0.0:
        return queue_ahead, 0.0
    queue_used = min(queue_ahead, compatible_size)
    queue_after = queue_ahead - queue_used
    residual = compatible_size - queue_used
    fill = min(remaining, residual)
    return queue_after, fill


def audit() -> dict[str, object]:
    delayed = TradeClock(event_ts_s=100, received_ms=105_500)
    arrival_ms = 99_500
    cancel_ms = 103_500
    event_queue, event_fill = consume(20.0, 10.0, 30.0, event_time_active(delayed, arrival_ms, cancel_ms))
    recv_queue, recv_fill = consume(20.0, 10.0, 30.0, receive_time_active(delayed, arrival_ms, cancel_ms))

    same_second = TradeClock(event_ts_s=100, received_ms=100_900)
    same_arrival_ms = 100_800
    same_cancel_ms = 103_500

    return {
        "delayed_observation": {
            "event_time_eligible": event_time_active(delayed, arrival_ms, cancel_ms),
            "receive_time_eligible": receive_time_active(delayed, arrival_ms, cancel_ms),
            "event_time_fill_shares": event_fill,
            "receive_time_fill_shares": recv_fill,
            "event_time_queue_after": event_queue,
            "receive_time_queue_after": recv_queue,
        },
        "same_second_ordering": {
            "event_time_eligible": event_time_active(same_second, same_arrival_ms, same_cancel_ms),
            "receive_time_eligible": receive_time_active(same_second, same_arrival_ms, same_cancel_ms),
        },
        "required_contract": [
            "use local received_ms for order-arrival/cancel causality",
            "retain exchange timestamp only as market-event metadata",
            "sort newly observed tape rows by received_ms before queue consumption",
            "never credit a delayed observation to an earlier fill window",
        ],
    }


def main() -> int:
    import json
    print(json.dumps(audit(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
