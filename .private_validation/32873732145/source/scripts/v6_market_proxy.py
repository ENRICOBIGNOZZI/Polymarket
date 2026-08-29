#!/usr/bin/env python3
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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

END = {"", "LTE=", "-1"}
FRESH_CACHE_SECONDS = 30.0
STALE_CACHE_SECONDS = 900.0
FALLBACK_MARKETS = 300
GAMMA_TIMEOUT_SECONDS = 1.5
CLOB_TIMEOUT_SECONDS = 6.0
CLOB_DISCOVERY_BUDGET_SECONDS = 14.0
BOOK_WORKERS = 8


def f(x: Any, d: float = 0.0) -> float:
    try:
        y = float(x)
    except (TypeError, ValueError, OverflowError):
        return d
    return y if math.isfinite(y) else d


def b(x: Any, d: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    if isinstance(x, str):
        return x.strip().lower() in {"1", "true", "yes"}
    return d


def atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def req(url: str, payload: Any | None = None, timeout: float = CLOB_TIMEOUT_SECONDS) -> Any:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "User-Agent": "polymarket-v6-market-proxy/2",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as handle:
            raw = handle.read()
            if str(handle.headers.get("Content-Encoding") or "").lower().strip() == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode())
    except (OSError, EOFError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"request failed: {url}: {exc}") from exc


def curl_req(url: str, payload: Any | None = None, timeout: float = CLOB_TIMEOUT_SECONDS) -> Any:
    binary = shutil.which("curl")
    if not binary:
        raise RuntimeError("curl unavailable")
    data = None if payload is None else json.dumps(payload, separators=(",", ":"))
    args = [
        binary,
        "--silent",
        "--show-error",
        "--fail",
        "--location",
        "--compressed",
        "--max-time",
        str(max(1, math.ceil(timeout))),
        "--user-agent",
        "polymarket-v6-market-proxy/2",
        "--header",
        "Accept: application/json",
    ]
    if data is not None:
        args.extend(["--header", "Content-Type: application/json", "--data-binary", data])
    args.append(url)
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            timeout=max(1.0, timeout + 1.0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"curl request failed: {url}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()[:240]
        raise RuntimeError(f"curl request failed: {url}: exit={completed.returncode} {detail}")
    try:
        return json.loads(completed.stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"curl returned invalid JSON: {url}: {exc}") from exc


def clob_req(url: str, payload: Any | None = None, timeout: float = CLOB_TIMEOUT_SECONDS) -> Any:
    try:
        return curl_req(url, payload, timeout)
    except RuntimeError as curl_error:
        try:
            return req(url, payload, timeout)
        except RuntimeError as urllib_error:
            raise RuntimeError(f"{curl_error}; urllib fallback: {urllib_error}") from urllib_error

def tokens(market: dict[str, Any]) -> list[dict[str, Any]]:
    value = market.get("tokens")
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def depth(book: dict[str, Any]) -> float:
    bids = book.get("bids") if isinstance(book.get("bids"), list) else []
    asks = book.get("asks") if isinstance(book.get("asks"), list) else []
    if not bids or not asks:
        return 0.0

    def side(rows: list[Any]) -> float:
        out = 0.0
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            price = f(row.get("price"), -1.0)
            size = f(row.get("size"), 0.0)
            if 0.0 < price < 1.0 and size > 0.0:
                out += price * size
        return out

    return min(side(bids), side(asks))


class Proxy:
    def __init__(self, gamma: str, clob: str, cache: Path, status: Path):
        self.gamma = gamma.rstrip("/")
        self.clob = clob.rstrip("/")
        self.cache = cache
        self.status = status
        self.state_lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.ts = 0.0
        self.idmap: dict[str, str] = {}
        self.exact: dict[str, tuple[float, Any]] = {}
        self.failures = 0
        self.error = ""
        self.source = "startup"
        self.clob_source = "startup"
        self.load()

    def load(self) -> None:
        try:
            value = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return
        markets = value.get("markets")
        if isinstance(markets, list):
            self.rows = [row for row in markets if isinstance(row, dict)]
            self.ts = f(value.get("timestamp"), 0.0)
        mapping = value.get("gamma_to_condition")
        if isinstance(mapping, dict):
            self.idmap = {str(k): str(v) for k, v in mapping.items() if k and v}

    def stat(self, source: str, n: int, upstream_ok: bool, age: float = 0.0) -> None:
        self.source = source
        atomic(
            self.status,
            {
                "schema": "polymarket_v6_market_proxy_status_v1",
                "timestamp": int(time.time()),
                "source": source,
                "markets": n,
                "upstream_gamma_ok": upstream_ok,
                "failures": self.failures,
                "last_error": self.error,
                "cache_age_seconds": max(0.0, age),
                "paper_only": True,
            },
        )

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
        atomic(
            self.cache,
            {
                "schema": "polymarket_v6_market_proxy_cache_v1",
                "timestamp": int(now),
                "markets": rows,
                "gamma_to_condition": mapping,
            },
        )

    def cached_page(self, limit: int, offset: int, min_liquidity: float, max_age: float) -> list[dict[str, Any]] | None:
        now = time.time()
        with self.state_lock:
            if not self.rows or now - self.ts > max_age:
                return None
            rows = [row for row in self.rows if f(row.get("liquidityNum")) >= min_liquidity]
            age = now - self.ts
            source = self.source
        page = rows[offset : offset + limit]
        self.stat(source + "_cache", len(rows), source.startswith("gamma"), age)
        return page

    def gamma_rows(self, n: int, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        params = {
            key: value[-1]
            for key, value in query.items()
            if value and key in {"active", "closed", "order", "ascending", "liquidity_num_min"}
        }
        params.setdefault("active", "true")
        params.setdefault("closed", "false")
        cursor = ""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        while len(out) < n and len(out) < FALLBACK_MARKETS:
            request_params = dict(params)
            request_params["limit"] = str(min(100, n - len(out)))
            if cursor:
                request_params["after_cursor"] = cursor
            value = req(
                self.gamma + "/markets/keyset?" + urllib.parse.urlencode(request_params),
                timeout=GAMMA_TIMEOUT_SECONDS,
            )
            if not isinstance(value, dict) or not isinstance(value.get("markets"), list):
                raise RuntimeError("bad Gamma keyset response")
            batch = [row for row in value["markets"] if isinstance(row, dict)]
            for row in batch:
                key = str(row.get("id") or row.get("conditionId") or "")
                if key and key not in seen:
                    seen.add(key)
                    out.append(row)
            nxt = str(value.get("next_cursor") or "")
            if not batch or nxt in END or nxt == cursor:
                break
            cursor = nxt
        if not out:
            raise RuntimeError("Gamma keyset returned no markets")
        return out[:n]

    def clob_candidates(self, n: int, path: str, deadline: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = ""
        source = path.strip("/") or "markets"
        while len(out) < n and time.monotonic() < deadline:
            params = {"next_cursor": cursor} if cursor else {}
            url = self.clob + path + ("?" + urllib.parse.urlencode(params) if params else "")
            remaining = deadline - time.monotonic()
            if remaining <= 0.1:
                break
            value = clob_req(url, timeout=min(CLOB_TIMEOUT_SECONDS, remaining))
            if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                raise RuntimeError(f"bad CLOB {source} response")
            batch = [row for row in value["data"] if isinstance(row, dict)]
            for row in batch:
                if not b(row.get("active"), True) or b(row.get("closed")) or b(row.get("archived")):
                    continue
                if not b(row.get("accepting_orders"), True):
                    continue
                if row.get("enable_order_book") is not None and not b(row.get("enable_order_book"), True):
                    continue
                if str(row.get("condition_id") or "") and len(tokens(row)) >= 2:
                    out.append(row)
                if len(out) >= n:
                    break
            nxt = str(value.get("next_cursor") or "")
            if not batch or nxt in END or nxt == cursor:
                break
            cursor = nxt
        if not out:
            raise RuntimeError(f"CLOB {source} returned no candidates")
        return out

    def books(self, candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        ids = [
            str(token.get("token_id") or "")
            for market in candidates
            for token in tokens(market)
            if str(token.get("token_id") or "")
        ]
        chunks = [ids[pos : pos + 80] for pos in range(0, len(ids), 80)]
        out: dict[str, dict[str, Any]] = {}

        def fetch(chunk: list[str]) -> Any:
            return clob_req(
                self.clob + "/books",
                [{"token_id": token_id} for token_id in chunk],
                timeout=CLOB_TIMEOUT_SECONDS,
            )

        with ThreadPoolExecutor(max_workers=min(BOOK_WORKERS, max(1, len(chunks)))) as pool:
            futures = [pool.submit(fetch, chunk) for chunk in chunks]
            for future in as_completed(futures):
                try:
                    value = future.result()
                except Exception:
                    continue
                if not isinstance(value, list):
                    continue
                for row in value:
                    if isinstance(row, dict) and str(row.get("asset_id") or ""):
                        out[str(row["asset_id"])] = row
        return out

    def convert(self, market: dict[str, Any], books: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        cid = str(market.get("condition_id") or "")
        market_tokens = tokens(market)
        ids = [str(token.get("token_id") or "") for token in market_tokens]
        outcomes = [str(token.get("outcome") or "") for token in market_tokens]
        if not cid or len(ids) < 2 or not ids[0] or not ids[1]:
            return None
        liquidity = min(depth(books.get(ids[0], {})), depth(books.get(ids[1], {})))
        if liquidity <= 0.0:
            return None
        return {
            "id": cid,
            "conditionId": cid,
            "eventId": cid,
            "slug": str(market.get("market_slug") or market.get("slug") or cid),
            "question": str(market.get("question") or ""),
            "liquidityNum": liquidity,
            "volume24hr": 0.0,
            "negRisk": b(market.get("neg_risk")),
            "active": b(market.get("active"), True),
            "closed": b(market.get("closed")),
            "enableOrderBook": b(market.get("enable_order_book"), True),
            "acceptingOrders": b(market.get("accepting_orders"), True),
            "clobTokenIds": ids,
            "outcomes": outcomes,
            "events": [],
            "_proxy_source": "clob",
        }

    def clob_rows_from(
        self, path: str, min_liquidity: float, deadline: float
    ) -> list[dict[str, Any]]:
        candidates = self.clob_candidates(FALLBACK_MARKETS, path, deadline)
        books = self.books(candidates)
        out: list[dict[str, Any]] = []
        for candidate in candidates:
            row = self.convert(candidate, books)
            if row and f(row.get("liquidityNum")) >= min_liquidity:
                out.append(row)
        out.sort(key=lambda row: f(row.get("liquidityNum")), reverse=True)
        if not out:
            raise RuntimeError(
                f"CLOB {path.strip('/') or 'markets'} found no two-sided liquid markets"
            )
        return out[:FALLBACK_MARKETS]

    def clob_rows(self, min_liquidity: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + CLOB_DISCOVERY_BUDGET_SECONDS
        errors: list[str] = []
        for path, source in (
            ("/sampling-markets", "clob_sampling"),
            ("/markets", "clob_markets"),
        ):
            try:
                rows = self.clob_rows_from(path, min_liquidity, deadline)
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                continue
            self.clob_source = source
            return rows
        self.clob_source = "clob_unavailable"
        raise RuntimeError("CLOB fallback failed: " + "; ".join(errors))

    def markets(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(f((query.get("limit") or [100])[-1], 100))))
        offset = max(0, int(f((query.get("offset") or [0])[-1], 0)))
        min_liquidity = f((query.get("liquidity_num_min") or [0])[-1], 0.0)

        cached = self.cached_page(limit, offset, min_liquidity, FRESH_CACHE_SECONDS)
        if cached is not None:
            return cached

        if not self.refresh_lock.acquire(blocking=False):
            cached = self.cached_page(limit, offset, min_liquidity, STALE_CACHE_SECONDS)
            if cached is not None:
                return cached
            if not self.refresh_lock.acquire(timeout=3.0):
                raise RuntimeError("market discovery refresh already in progress")
            self.refresh_lock.release()
            cached = self.cached_page(limit, offset, min_liquidity, STALE_CACHE_SECONDS)
            if cached is not None:
                return cached
            raise RuntimeError("market discovery refresh completed without usable cache")

        try:
            target = min(FALLBACK_MARKETS, max(FALLBACK_MARKETS, offset + limit))
            try:
                rows = self.gamma_rows(target, query)
                self.error = ""
                self.save(rows)
                self.stat("gamma_keyset", len(rows), True)
            except Exception as gamma_error:
                self.failures += 1
                self.error = str(gamma_error)
                try:
                    rows = self.clob_rows(min_liquidity)
                    self.save(rows)
                    self.stat(self.clob_source, len(rows), False)
                except Exception as clob_error:
                    self.failures += 1
                    self.error = f"{self.error}; {clob_error}"
                    cached = self.cached_page(limit, offset, min_liquidity, STALE_CACHE_SECONDS)
                    if cached is not None:
                        self.stat("stale_cache", len(self.rows), False, max(0.0, time.time() - self.ts))
                        return cached
                    self.stat("unavailable", 0, False, 1e12)
                    raise RuntimeError(self.error or "discovery unavailable")
        finally:
            self.refresh_lock.release()

        cached = self.cached_page(limit, offset, min_liquidity, FRESH_CACHE_SECONDS)
        return cached if cached is not None else []

    def cached_market(self, mid: str) -> dict[str, Any] | None:
        with self.state_lock:
            for row in self.rows:
                if str(row.get("id") or "") == mid or str(row.get("conditionId") or "") == mid:
                    return dict(row)
        return None

    def one(self, mid: str) -> dict[str, Any]:
        cached_market = self.cached_market(mid)
        if cached_market is not None:
            return cached_market
        path = "/markets/" + urllib.parse.quote(mid, safe="")
        try:
            value = req(self.gamma + path, timeout=GAMMA_TIMEOUT_SECONDS)
            if isinstance(value, dict):
                cid = str(value.get("conditionId") or "")
                gamma_id = str(value.get("id") or mid)
                if cid:
                    self.idmap[gamma_id] = cid
                self.exact[path] = (time.time(), value)
                return value
        except Exception as exc:
            self.failures += 1
            self.error = str(exc)
        cached = self.exact.get(path)
        if cached and time.time() - cached[0] <= STALE_CACHE_SECONDS and isinstance(cached[1], dict):
            return cached[1]
        cid = self.idmap.get(mid, mid)
        value = clob_req(
            self.clob + "/markets/" + urllib.parse.quote(cid, safe=""),
            timeout=CLOB_TIMEOUT_SECONDS,
        )
        if not isinstance(value, dict):
            raise RuntimeError("bad CLOB single market")
        converted = self.convert(value, self.books([value]))
        if not converted:
            raise RuntimeError("CLOB single market has no valid two-sided book")
        return converted

    def generic(self, path_query: str) -> Any:
        try:
            value = req(self.gamma + path_query, timeout=GAMMA_TIMEOUT_SECONDS)
            self.exact[path_query] = (time.time(), value)
            return value
        except Exception as exc:
            self.failures += 1
            self.error = str(exc)
        cached = self.exact.get(path_query)
        if cached and time.time() - cached[0] <= STALE_CACHE_SECONDS:
            return cached[1]
        raise RuntimeError(self.error or "Gamma unavailable")


class H(BaseHTTPRequestHandler):
    proxy: Proxy

    def log_message(self, *_: Any) -> None:
        return

    def sendj(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            self.sendj(200, {"ok": True, "source": self.proxy.source, "failures": self.proxy.failures})
            return
        try:
            if url.path in {"/markets", "/markets/keyset"}:
                rows = self.proxy.markets(urllib.parse.parse_qs(url.query, keep_blank_values=True))
                payload: Any = {"markets": rows, "next_cursor": ""} if url.path.endswith("keyset") else rows
                self.sendj(200, payload)
                return
            if url.path.startswith("/markets/") and url.path.count("/") == 2:
                self.sendj(200, self.proxy.one(urllib.parse.unquote(url.path.rsplit("/", 1)[-1])))
                return
            self.sendj(200, self.proxy.generic(self.path))
        except Exception as exc:
            self.sendj(503, {"error": "market_proxy_unavailable", "detail": str(exc)[:400]})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9120)
    parser.add_argument("--gamma", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob", default="https://clob.polymarket.com")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    H.proxy = Proxy(args.gamma, args.clob, args.cache, args.status)
    server = ThreadingHTTPServer((args.host, args.port), H)
    print(f"v6 market proxy listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
