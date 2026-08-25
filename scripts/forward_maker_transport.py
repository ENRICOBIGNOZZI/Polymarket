#!/usr/bin/env python3
"""Bounded transport hardening for the read-only forward maker probe.

The probe's HTTP helper already retries normal URL/HTTP/timeout/JSON failures.
Python's chunked-response reader can additionally raise ``IncompleteRead`` or
``RemoteDisconnected`` while consuming a response body. Those are transport
failures, not valid partial evidence, so retry the complete idempotent request
and fail closed after the same bounded attempt budget.
"""
from __future__ import annotations

import http.client
import time
from typing import Any, Callable

import forward_maker_probe as probe

_TRANSIENT_READ_ERRORS = (
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    BrokenPipeError,
)


def install_resilient_request_json(
    module: Any = probe,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[..., Any]:
    """Wrap ``module.request_json`` with full-request retries for read failures."""
    original = module.request_json

    def resilient_request_json(
        url: str,
        *,
        method: str = "GET",
        body: Any | None = None,
        timeout: float = 20.0,
        retries: int = 3,
    ) -> Any:
        attempts = max(1, int(retries))
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                # Let the original helper perform one complete request. Its
                # RuntimeError covers URL/HTTP/timeout/JSON failures; the read
                # exceptions below are the gap that caused the scheduler crash.
                return original(
                    url,
                    method=method,
                    body=body,
                    timeout=timeout,
                    retries=1,
                )
            except _TRANSIENT_READ_ERRORS + (RuntimeError,) as exc:
                last = exc
                if attempt + 1 < attempts:
                    sleep(min(4.0, 0.5 * 2**attempt))
        raise RuntimeError(f"request failed after {attempts} attempts: {url}: {last}")

    module.request_json = resilient_request_json
    return resilient_request_json


def main() -> int:
    install_resilient_request_json(probe)
    return probe.main()


if __name__ == "__main__":
    raise SystemExit(main())
