#!/usr/bin/env python3
"""Reconciled V6 market proxy entrypoint.

This module keeps the currently deployed non-blocking stale-cache reader intact while
repairing atomic relay consumption.  The hosted relay installs a validated cache by
rename, so file identity rather than monotone mtime is the correct change detector.
The split is deliberately narrow: ``v6_market_proxy_base.py`` is the exact previous
implementation and this shim overrides only cache identity/save semantics and the
bounded IPv4 Gamma transport.  It can be collapsed back into one file after the
runtime incident is closed without changing behavior.
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any

_BASE_PATH = Path(__file__).with_name("v6_market_proxy_base.py")
_SPEC = importlib.util.spec_from_file_location("_polymarket_v6_market_proxy_base", _BASE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load V6 market proxy base: {_BASE_PATH}")
_impl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_impl)

# Preserve the public surface used by existing tests and runtime tooling.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

_ORIGINAL_REQ = _impl.req
MAX_CACHE_FUTURE_SKEW_SECONDS = 30.0


def gamma_req(url: str, payload: Any | None = None, timeout: float = _impl.GAMMA_TIMEOUT_SECONDS) -> Any:
    """Use the same bounded compressed IPv4 transport policy as CLOB for Gamma."""
    if _impl.shutil.which("curl"):
        return _impl.curl_req(url, payload, timeout)
    return _ORIGINAL_REQ(url, payload, timeout)


class Proxy(_impl.Proxy):
    """Current proxy plus inode-aware atomic relay reload semantics."""

    def __init__(self, gamma: str, clob: str, cache: Path, status: Path):
        # Base __init__ calls self.load(), so initialize the new identity field first.
        self.cache_signature: tuple[int, int, int, int] | None = None
        super().__init__(gamma, clob, cache, status)

    def load(self) -> bool:
        try:
            metadata = self.cache.stat()
            signature = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
            )
            if signature == self.cache_signature:
                return False
            value = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict) or value.get("schema") != _impl.CACHE_SCHEMA:
            return False
        markets = value.get("markets")
        timestamp = _impl.f(value.get("timestamp"), 0.0)
        if not isinstance(markets, list) or timestamp <= 0.0:
            return False
        if timestamp > time.time() + MAX_CACHE_FUTURE_SKEW_SECONDS:
            return False
        if not markets or not all(_impl.valid_cache_market(row) for row in markets):
            return False
        rows = [dict(row) for row in markets]
        mapping = value.get("gamma_to_condition")
        with self.state_lock:
            # Integer cache timestamps may be equal across two atomic publishes.  Do
            # not roll materially backward, but do accept a different inode in the
            # same second.
            if timestamp + 1.0 < self.ts:
                self.cache_signature = signature
                return False
            self.rows = rows
            self.ts = timestamp
            if isinstance(mapping, dict):
                self.idmap.update({str(k): str(v) for k, v in mapping.items() if k and v})
            self.cache_signature = signature
        return True

    def save(self, rows: list[dict[str, Any]]) -> None:
        now = time.time()
        with self.state_lock:
            self.rows = rows
            self.ts = now
            for row in rows:
                mid = str(row.get("id") or "")
                cid = str(row.get("conditionId") or "")
                if mid and cid:
                    self.idmap[mid] = cid
            mapping = dict(self.idmap)
        _impl.atomic(
            self.cache,
            {
                "schema": _impl.CACHE_SCHEMA,
                "timestamp": int(now),
                "markets": rows,
                "gamma_to_condition": mapping,
            },
        )
        # Never stat after replace: the hosted relay can win the race between the
        # local write and stat().  Clearing the signature forces one validated reread
        # and prevents associating relay inode identity with stale in-memory rows.
        with self.state_lock:
            self.cache_signature = None


# Base methods resolve ``req`` and ``Proxy`` through their module globals.  Rebind
# only those names so we retain the latest non-blocking stale-cache implementation.
_impl.req = gamma_req
_impl.Proxy = Proxy
H = _impl.H


def main() -> int:
    return _impl.main()


if __name__ == "__main__":
    raise SystemExit(main())
