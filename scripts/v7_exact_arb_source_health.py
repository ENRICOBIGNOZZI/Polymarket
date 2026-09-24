"""Bounded source leases for live observers; replay supplies its own as-of clock."""
from __future__ import annotations

UNIVERSE_MAX_AGE_MS = 900_000
HOTSET_MAX_AGE_MS = 120_000


def lease(timestamp_ms, now_ms, maximum_age_ms, label):
    if type(timestamp_ms) is not int or timestamp_ms <= 0 or not 0 <= now_ms - timestamp_ms <= maximum_age_ms:
        raise ValueError(label + "_expired_or_invalid_timestamp")
    return timestamp_ms + maximum_age_ms


def universe_lease(universe, now_ms):
    if universe.get("source_valid") is not True:
        raise ValueError("universe_source_invalid")
    return lease(universe.get("timestamp_ms"), now_ms, UNIVERSE_MAX_AGE_MS, "universe")


def graph_lease(graph, now_ms):
    if graph.get("source_universe_valid") is not True:
        raise ValueError("graph_source_invalid")
    return lease(graph.get("source_universe_timestamp_ms"), now_ms, UNIVERSE_MAX_AGE_MS, "graph")
