#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_external_fair_tape_replay import (  # noqa: E402
    EXTERNAL_TOPIC, ORACLE_TOPIC, replay, structural_probability,
)


def event(topic: str, source_seconds: int, receive_seconds: float, price: float) -> dict:
    return {
        "topic": topic, "timestamp_ms": source_seconds * 1000,
        "receive_wall_ns": int(receive_seconds * 1_000_000_000), "price": price,
    }


def main() -> None:
    start, end = 1_800, 2_100
    events = [
        event(ORACLE_TOPIC, start, start + 0.1, 100.0),
        event(EXTERNAL_TOPIC, start + 1, start + 1.1, 100.1),
        event(ORACLE_TOPIC, start + 239, start + 239.1, 100.2),
        event(EXTERNAL_TOPIC, start + 239, start + 239.2, 100.3),
        event(ORACLE_TOPIC, end - 6, end - 5.9, 100.8),
        event(EXTERNAL_TOPIC, end - 6, end - 5.8, 100.9),
        event(ORACLE_TOPIC, end, end + 0.1, 101.0),
    ]
    result = replay(events, buckets=(60.0, 5.0))
    assert result["contracts"] == 1
    assert result["observations"] == 2
    assert result["limitations"] == ["PUBLIC_RTDS_SINGLE_EXTERNAL_LEG", "NOT_EXECUTION_PROMOTION_EVIDENCE"]
    assert all(row["contracts"] == 1 for row in result["buckets"])
    assert all(row["directional_accuracy"] == 1.0 for row in result["buckets"])
    assert structural_probability(100.0, 100.2, 100.3, 60.0) > 0.5

    # An exact terminal boundary alone is insufficient: the opening reference
    # must also exist and have arrived causally before the forecast cutoff.
    no_opening = replay(events[1:], buckets=(60.0,))
    assert no_opening["contracts"] == 0 and no_opening["observations"] == 0


if __name__ == "__main__":
    main()
