#!/usr/bin/env python3
from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from typing import Any

PM_CLOB_HOST = "clob.polymarket.com"
KALSHI_HOST = "external-api.kalshi.com"
GDELT_HOST = "api.gdeltproject.org"


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


def wrap_request_json(
    delegate: Callable[..., Any],
    *,
    gdelt_min_interval_seconds: float = 1.25,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[..., Any]:
    last_gdelt_request: float | None = None

    def request(url: str, *, timeout: float = 20.0, retries: int = 3) -> Any:
        nonlocal last_gdelt_request
        normalized = rewrite_external_url(url)
        host = urllib.parse.urlsplit(normalized).hostname
        if host == GDELT_HOST and gdelt_min_interval_seconds > 0:
            now = monotonic()
            if last_gdelt_request is not None:
                remaining = gdelt_min_interval_seconds - (now - last_gdelt_request)
                if remaining > 0:
                    sleep(remaining)
            try:
                return delegate(normalized, timeout=timeout, retries=retries)
            finally:
                last_gdelt_request = monotonic()
        return delegate(normalized, timeout=timeout, retries=retries)

    return request
