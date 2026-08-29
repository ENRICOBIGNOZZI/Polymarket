#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import urllib.error
from typing import Any

import v7_cross_sectional_rank as base

DEFAULT_MAX_WORKERS = 6


def _fetch_batch(
    clob: str,
    batch: list[str],
    token_to_market: dict[str, str],
    window_start: int,
    window_end: int,
    fidelity_minutes: int,
) -> tuple[dict[str, dict[int, float]], list[str]]:
    out: dict[str, dict[int, float]] = {}
    failures: list[str] = []
    try:
        raw = base.request_json(
            clob.rstrip("/") + "/batch-prices-history",
            {
                "markets": batch,
                "start_ts": window_start,
                "end_ts": window_end,
                "fidelity": fidelity_minutes,
            },
        )
        history = raw.get("history", {}) if isinstance(raw, dict) else {}
        if isinstance(history, dict):
            for token, rows in history.items():
                market_id = token_to_market.get(str(token))
                if market_id and isinstance(rows, list):
                    parsed = base.parse_history(rows, fidelity_minutes)
                    if parsed:
                        out[market_id] = parsed
    except Exception as exc:
        detail = base.http_error_detail(exc)
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            raise RuntimeError(
                f"price history rate limited at {window_start}-{window_end}: {detail}"
            ) from exc
        failures.append(f"batch:{window_start}:{window_end}:{detail}")
    return out, failures


def fetch_histories(
    clob: str,
    markets: list[Any],
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> tuple[dict[str, dict[int, float]], list[str]]:
    """Bounded concurrent batch-only history transport for V7 ranking evidence.

    The statistical sample, 7-day absolute windows, fidelity and endpoint payload are
    identical to the canonical research adapter. Only independent read-only batch
    calls are concurrent. Results are merged by market/timestamp after each future
    completes, so execution order cannot affect the final panel.
    """
    if end_ts <= start_ts or not markets:
        return {}, []
    token_to_market = {str(market.yes_token): str(market.market_id) for market in markets}
    tokens = list(token_to_market)
    tasks: list[tuple[list[str], int, int]] = []
    window_start = start_ts
    while window_start < end_ts:
        window_end = min(end_ts, window_start + base.HISTORY_WINDOW_SECONDS)
        for index in range(0, len(tokens), base.HISTORY_BATCH_SIZE):
            tasks.append((tokens[index : index + base.HISTORY_BATCH_SIZE], window_start, window_end))
        window_start = window_end

    histories: dict[str, dict[int, float]] = {}
    failures: list[str] = []
    workers = max(1, min(int(max_workers), 8, len(tasks))) if tasks else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _fetch_batch,
                clob,
                batch,
                token_to_market,
                window_start,
                window_end,
                fidelity_minutes,
            )
            for batch, window_start, window_end in tasks
        ]
        for future in concurrent.futures.as_completed(futures):
            partial, partial_failures = future.result()
            failures.extend(partial_failures)
            for market_id, series in partial.items():
                histories.setdefault(market_id, {}).update(series)
    return histories, sorted(failures)
