#!/usr/bin/env python3
"""Canonical V7 private market proxy.

The implementation preserves the validated bounded stale-cache, Gamma/CLOB fallback,
IPv4 transport and atomic relay semantics, but all persisted/runtime contracts are V7.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import v7_market_proxy_base as base

CACHE_SCHEMA = "polymarket_v7_market_proxy_cache_v1"
STATUS_SCHEMA = "polymarket_v7_market_proxy_status_v1"
MAX_CACHE_FUTURE_SKEW_SECONDS = 30.0


def request_json(url: str, payload: Any | None = None, timeout: float = base.CLOB_TIMEOUT_SECONDS) -> Any:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "User-Agent": "polymarket-v7-market-proxy/1",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as handle:
            raw = handle.read()
            if str(handle.headers.get("Content-Encoding") or "").lower().strip() == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode())
    except (OSError, EOFError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"request failed: {url}: {exc}") from exc


def curl_request(url: str, payload: Any | None = None, timeout: float = base.CLOB_TIMEOUT_SECONDS) -> Any:
    binary = shutil.which("curl")
    if not binary:
        raise RuntimeError("curl unavailable")
    data = None if payload is None else json.dumps(payload, separators=(",", ":"))
    args = [
        binary, "--silent", "--show-error", "--fail", "--location", "--ipv4", "--compressed",
        "--max-time", str(max(1, math.ceil(timeout))),
        "--user-agent", "polymarket-v7-market-proxy/1",
        "--header", "Accept: application/json",
    ]
    if data is not None:
        args.extend(["--header", "Content-Type: application/json", "--data-binary", data])
    args.append(url)
    try:
        completed = subprocess.run(args, check=False, capture_output=True, timeout=max(1.0, timeout + 1.0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"curl request failed: {url}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()[:240]
        raise RuntimeError(f"curl request failed: {url}: exit={completed.returncode} {detail}")
    try:
        return json.loads(completed.stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"curl returned invalid JSON: {url}: {exc}") from exc


def clob_request(url: str, payload: Any | None = None, timeout: float = base.CLOB_TIMEOUT_SECONDS) -> Any:
    return curl_request(url, payload, timeout) if shutil.which("curl") else request_json(url, payload, timeout)


class Proxy(base.Proxy):
    def __init__(self, gamma: str, clob: str, cache: Path, status: Path):
        self.cache_signature: tuple[int, int, int, int] | None = None
        super().__init__(gamma, clob, cache, status)

    def load(self) -> bool:
        try:
            metadata = self.cache.stat()
            signature = (int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_size), int(metadata.st_mtime_ns))
            if signature == self.cache_signature:
                return False
            value = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA:
            return False
        markets = value.get("markets")
        timestamp = base.f(value.get("timestamp"), 0.0)
        if not isinstance(markets, list) or timestamp <= 0.0:
            return False
        if timestamp > time.time() + MAX_CACHE_FUTURE_SKEW_SECONDS:
            return False
        if not markets or not all(base.valid_cache_market(row) for row in markets):
            return False
        rows = [dict(row) for row in markets]
        mapping = value.get("gamma_to_condition")
        with self.state_lock:
            if timestamp + 1.0 < self.ts:
                self.cache_signature = signature
                return False
            self.rows = rows
            self.ts = timestamp
            if isinstance(mapping, dict):
                self.idmap.update({str(key): str(value) for key, value in mapping.items() if key and value})
            self.cache_signature = signature
        return True

    def save(self, rows: list[dict[str, Any]]) -> None:
        now = time.time()
        with self.state_lock:
            self.rows = rows
            self.ts = now
            for row in rows:
                market_id = str(row.get("id") or "")
                condition_id = str(row.get("conditionId") or "")
                if market_id and condition_id:
                    self.idmap[market_id] = condition_id
            mapping = dict(self.idmap)
        base.atomic(self.cache, {
            "schema": CACHE_SCHEMA,
            "timestamp": int(now),
            "markets": rows,
            "gamma_to_condition": mapping,
        })
        with self.state_lock:
            self.cache_signature = None

    def stat(self, source: str, count: int, upstream_ok: bool, age: float = 0.0) -> None:
        self.source = source
        base.atomic(self.status, {
            "schema": STATUS_SCHEMA,
            "timestamp": int(time.time()),
            "source": source,
            "markets": count,
            "upstream_gamma_ok": upstream_ok,
            "failures": self.failures,
            "last_error": self.error,
            "cache_age_seconds": max(0.0, age),
            "paper_only": True,
        })

    def _refresh_stale_cache_in_background(
        self,
        query: dict[str, list[str]],
        limit: int,
        offset: int,
        min_liquidity: float,
    ) -> None:
        def refresh() -> None:
            try:
                self._refresh_locked(query, limit, offset, min_liquidity)
            except Exception:
                pass
            finally:
                self.refresh_lock.release()
        worker = threading.Thread(target=refresh, name="v7-market-refresh", daemon=True)
        try:
            worker.start()
        except Exception:
            self.refresh_lock.release()
            raise


class ReusableThreadingHTTPServer(base.ThreadingHTTPServer):
    allow_reuse_address = True


# Base methods resolve transport and class symbols through their module globals.
base.CACHE_SCHEMA = CACHE_SCHEMA
base.req = request_json
base.curl_req = curl_request
base.clob_req = clob_request
base.Proxy = Proxy
base.ThreadingHTTPServer = ReusableThreadingHTTPServer
H = base.H


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 bounded private market proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9120)
    parser.add_argument("--gamma", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob", default="https://clob.polymarket.com")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    H.proxy = Proxy(args.gamma, args.clob, args.cache, args.status)
    server = ReusableThreadingHTTPServer((args.host, args.port), H)
    print(f"v7 market proxy listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
