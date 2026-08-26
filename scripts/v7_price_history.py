#!/usr/bin/env python3
from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any


RequestJson = Callable[[str, Any | None], Any]
ParseHistory = Callable[[list[Any], int], dict[int, float]]


def bounded_windows(start: int, end: int, maximum_window_seconds: int) -> list[tuple[int, int]]:
    lo = int(start)
    stop = int(end)
    width = max(3600, int(maximum_window_seconds))
    if stop <= lo:
        return []
    out: list[tuple[int, int]] = []
    while lo < stop:
        hi = min(stop, lo + width)
        out.append((lo, hi))
        lo = hi
    return out


def _merge_history(target: dict[int, float], incoming: Mapping[int, float]) -> None:
    for timestamp, value in incoming.items():
        target[int(timestamp)] = float(value)


def fetch_histories_chunked(
    clob: str,
    token_by_market: Mapping[str, str],
    start: int,
    end: int,
    fidelity: int,
    request_json: RequestJson,
    parse_history: ParseHistory,
    *,
    maximum_window_seconds: int = 6 * 24 * 3600,
    batch_size: int = 20,
    maximum_single_fallback_requests: int = 120,
) -> tuple[dict[str, dict[int, float]], list[str], dict[str, int]]:
    """Fetch an exact absolute history window without sending one oversized request.

    The CLOB history endpoint accepts explicit start/end timestamps, but long absolute
    windows can be rejected.  Split the requested chronology into bounded windows,
    preserve the same fidelity, and merge by timestamp.  Batch failures fall back to
    bounded token-level requests for the same exact chunk; no interval shorthand or
    current-time-relative window is substituted.
    """
    root = clob.rstrip("/")
    token_to_market = {str(token): str(mid) for mid, token in token_by_market.items() if token}
    tokens = list(token_to_market)
    windows = bounded_windows(start, end, maximum_window_seconds)
    out: dict[str, dict[int, float]] = {}
    failures: list[str] = []
    batch_requests = 0
    fallback_requests = 0

    for lo, hi in windows:
        for offset in range(0, len(tokens), max(1, int(batch_size))):
            batch = tokens[offset : offset + max(1, int(batch_size))]
            batch_requests += 1
            failed = False
            try:
                raw = request_json(
                    root + "/batch-prices-history",
                    {
                        "markets": batch,
                        "start_ts": lo,
                        "end_ts": hi,
                        "fidelity": int(fidelity),
                    },
                )
                history = raw.get("history", {}) if isinstance(raw, dict) else {}
                if not isinstance(history, dict):
                    raise ValueError("batch history response missing history object")
                for token, rows in history.items():
                    mid = token_to_market.get(str(token))
                    if mid is None or not isinstance(rows, list):
                        continue
                    parsed = parse_history(rows, int(fidelity))
                    if parsed:
                        _merge_history(out.setdefault(mid, {}), parsed)
            except Exception as exc:
                failed = True
                failures.append(f"batch:{lo}:{hi}:{type(exc).__name__}")

            if not failed:
                continue
            for token in batch:
                if fallback_requests >= max(0, int(maximum_single_fallback_requests)):
                    failures.append("single_fallback_budget_exhausted")
                    break
                fallback_requests += 1
                mid = token_to_market[token]
                try:
                    query = urllib.parse.urlencode(
                        {
                            "market": token,
                            "startTs": lo,
                            "endTs": hi,
                            "fidelity": int(fidelity),
                        }
                    )
                    raw = request_json(root + "/prices-history?" + query, None)
                    rows = raw.get("history", []) if isinstance(raw, dict) else []
                    parsed = parse_history(rows if isinstance(rows, list) else [], int(fidelity))
                    if parsed:
                        _merge_history(out.setdefault(mid, {}), parsed)
                except Exception as exc:
                    failures.append(f"single:{mid}:{lo}:{hi}:{type(exc).__name__}")
            if fallback_requests >= max(0, int(maximum_single_fallback_requests)):
                break
        if fallback_requests >= max(0, int(maximum_single_fallback_requests)):
            break

    diagnostics = {
        "windows": len(windows),
        "batch_requests": batch_requests,
        "single_fallback_requests": fallback_requests,
        "markets_with_history": len(out),
    }
    return out, failures, diagnostics
