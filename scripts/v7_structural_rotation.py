#!/usr/bin/env python3
"""Deterministic bounded rotation for canonical structural research scans."""
from __future__ import annotations

from collections.abc import Sequence


def rotating_window(values: Sequence[str], cursor: int, budget: int) -> tuple[list[str], int]:
    """Return at most ``budget`` items and a cursor that eventually covers all values."""
    if budget <= 0 or not values:
        return [], 0
    count = min(len(values), budget)
    start = cursor % len(values)
    selected = [values[(start + offset) % len(values)] for offset in range(count)]
    return selected, (start + count) % len(values)
