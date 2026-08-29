#!/usr/bin/env python3
from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from typing import Any

PM_CLOB_HOST = "clob.polymarket.com"
KALSHI_HOST = "external-api.kalshi.com"
GDELT_HOST = "api.gdeltproject.org"
DEFAULT_CLOB_HISTORY_WINDOW_SECONDS = 7 * 86400
DEFAULT_GDELT_MIN_INTERVAL_SECONDS = 15.0
DEFAULT_GDELT_RATE_LIMIT_BACKOFF_SECONDS = 45.0
GDELT_TRANSIENT_MARKERS = ("429", "too many requests", "expecting value")


def _query_pairs(url: str) -> tuple[urllib.parse.SplitResult, list[tuple[str, str]]]:
    parsed = urllib.parse.urlsplit(url)
    return parsed, urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)


def rewrite_external_url(url: str) -> str:
    parsed, pairs = _query_pairs(url)
    changed = False

    if parsed.hostname == PM_CLOB_HOST and parsed.path.rstrip("/") == "/prices-history":
        keys = {key for key, _ in pairs}
        if "startTs" in keys and "endTs" in keys and "interval" in keys:
            pairs = [(key, value) for key, value in pairs if key != "interval"]
            changed = True

    if parsed.hostname == KALSHI_HOST and parsed.path.rstrip("/").endswith("/trade-api/v2/markets"):
        pairs = [(key, value) for key, value in pairs if key != "mve_filter"]
        pairs.append(("mve_filter", "exclude"))
        changed = True

    if not changed:
        return url
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(pairs), parsed.fragment)
    )


def _clob_history_window(url: str) -> tuple[int, int] | None:
    parsed, pairs = _query_pairs(url)
    if parsed.hostname != PM_CLOB_HOST or parsed.path.rstrip("/") != "/prices-history":
        return None
    query = dict(pairs)
    try:
        start_ts = int(query["startTs"])
        end_ts = int(query["endTs"])
    except (KeyError, TypeError, ValueError):
        return None
    if start_ts <= 0 or end_ts <= start_ts:
        return None
    return start_ts, end_ts


def _replace_window(url: str, start_ts: int, end_ts: int) -> str:
    parsed, pairs = _query_pairs(url)
    rewritten: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "startTs":
            value = str(start_ts)
        elif key == "endTs":
            value = str(end_ts)
        rewritten.append((key, value))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(rewritten), parsed.fragment)
    )


def _merge_history_payloads(payloads: list[Any]) -> dict[str, Any]:
    by_timestamp: dict[int, dict[str, Any]] = {}
    untimestamped: list[dict[str, Any]] = []
    for payload in payloads:
        history = payload.get("history") if isinstance(payload, dict) else None
        if not isinstance(history, list):
            continue
        for row in history:
            if not isinstance(row, dict):
                continue
            try:
                timestamp = int(float(row.get("t")))
            except (TypeError, ValueError, OverflowError):
                untimestamped.append(row)
                continue
            by_timestamp[timestamp] = row
    merged = [by_timestamp[key] for key in sorted(by_timestamp)]
    merged.extend(untimestamped)
    return {"history": merged}


def wrap_request_json(
    delegate: Callable[..., Any],
    *,
    gdelt_min_interval_seconds: float = DEFAULT_GDELT_MIN_INTERVAL_SECONDS,
    gdelt_rate_limit_backoff_seconds: float = DEFAULT_GDELT_RATE_LIMIT_BACKOFF_SECONDS,
    clob_history_window_seconds: int = DEFAULT_CLOB_HISTORY_WINDOW_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[..., Any]:
    last_gdelt_request: float | None = None

    def request(url: str, *, timeout: float = 20.0, retries: int = 3) -> Any:
        nonlocal last_gdelt_request
        normalized = rewrite_external_url(url)
        host = urllib.parse.urlsplit(normalized).hostname

        window = _clob_history_window(normalized)
        if window is not None and clob_history_window_seconds > 0:
            start_ts, end_ts = window
            if end_ts - start_ts > clob_history_window_seconds:
                payloads: list[Any] = []
                cursor = start_ts
                while cursor < end_ts:
                    chunk_end = min(end_ts, cursor + clob_history_window_seconds)
                    payloads.append(delegate(
                        _replace_window(normalized, cursor, chunk_end),
                        timeout=timeout,
                        retries=retries,
                    ))
                    cursor = chunk_end
                return _merge_history_payloads(payloads)

        if host == GDELT_HOST:
            if gdelt_min_interval_seconds > 0:
                now = monotonic()
                if last_gdelt_request is not None:
                    remaining = gdelt_min_interval_seconds - (now - last_gdelt_request)
                    if remaining > 0:
                        sleep(remaining)
            try:
                try:
                    # GDELT rate limiting is burst-sensitive. The wrapped delegate
                    # normally retries quickly; disable that inner burst and let this
                    # policy own the single long-backoff retry instead.
                    return delegate(normalized, timeout=timeout, retries=1)
                except RuntimeError as exc:
                    text = str(exc).lower()
                    retryable = any(marker in text for marker in GDELT_TRANSIENT_MARKERS)
                    if gdelt_rate_limit_backoff_seconds <= 0 or not retryable:
                        raise
                    sleep(gdelt_rate_limit_backoff_seconds)
                    return delegate(normalized, timeout=timeout, retries=1)
            finally:
                last_gdelt_request = monotonic()
        return delegate(normalized, timeout=timeout, retries=retries)

    return request
