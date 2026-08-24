#!/usr/bin/env python3
"""Read-only authenticated Polymarket account reconciliation.

Credentials are read exclusively from environment variables. The script never
prints secret material and performs GET requests only. It inventories open
orders and the first authenticated trade-history page so paper fill assumptions
can later be compared with exchange-confirmed account evidence.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENV_ADDRESS = "POLYMARKET_ADDRESS"
ENV_API_KEY = "POLYMARKET_API_KEY"
ENV_API_SECRET = "POLYMARKET_API_SECRET"
ENV_PASSPHRASE = "POLYMARKET_PASSPHRASE"


def decode_secret(raw: str) -> bytes:
    value = raw.strip().encode("ascii")
    value += b"=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value)


def l2_signature(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    if method.upper() != "GET":
        raise ValueError("read-only account probe permits GET only")
    message = (timestamp + method.upper() + path + body).encode("utf-8")
    digest = hmac.new(decode_secret(secret), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def credentials() -> dict[str, str] | None:
    values = {
        "address": os.environ.get(ENV_ADDRESS, "").strip(),
        "api_key": os.environ.get(ENV_API_KEY, "").strip(),
        "secret": os.environ.get(ENV_API_SECRET, "").strip(),
        "passphrase": os.environ.get(ENV_PASSPHRASE, "").strip(),
    }
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("incomplete Polymarket account environment: " + ",".join(missing))
    return values


def authenticated_get(base_url: str, path: str, creds: dict[str, str], timeout: float) -> Any:
    if not path.startswith("/") or "?" in path:
        raise ValueError("read-only reconciliation requires a canonical query-free GET path")
    timestamp = str(int(time.time()))
    signature = l2_signature(creds["secret"], timestamp, "GET", path)
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        method="GET",
        headers={
            "POLY_ADDRESS": creds["address"],
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": creds["api_key"],
            "POLY_PASSPHRASE": creds["passphrase"],
            "User-Agent": "polymarket-account-readonly/1.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_open_orders(base_url: str, creds: dict[str, str], timeout: float) -> Any:
    return authenticated_get(base_url, "/data/orders", creds, timeout)


def get_trades(base_url: str, creds: dict[str, str], timeout: float) -> Any:
    return authenticated_get(base_url, "/trades", creds, timeout)


def data_rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def count_orders(payload: Any) -> int:
    return len(data_rows(payload, "orders", "data"))


def trade_summary(payload: Any) -> tuple[int, int, int | None]:
    rows = data_rows(payload, "trades", "data")
    confirmed = 0
    latest: int | None = None
    for row in rows:
        status = str(row.get("status") or "").upper()
        if status in {"TRADE_STATUS_CONFIRMED", "CONFIRMED"}:
            confirmed += 1
        for key in ("match_time", "last_update", "timestamp"):
            try:
                ts = int(float(row.get(key)))
            except (TypeError, ValueError):
                continue
            if ts > 0:
                latest = ts if latest is None else max(latest, ts)
                break
    return len(rows), confirmed, latest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--output", type=Path, default=Path("runs/paper_v4_live/account_readonly_status.json"))
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    generated = int(time.time())
    creds = credentials()
    if creds is None:
        status = {
            "schema": "polymarket_account_readonly_v2",
            "generated_ts": generated,
            "configured": False,
            "read_only": True,
            "authenticated_execution": False,
            "open_orders": None,
            "trade_page_count": None,
            "confirmed_trade_page_count": None,
            "latest_trade_ts": None,
            "status": "not_configured",
        }
        atomic_json(args.output, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    try:
        orders = get_open_orders(args.clob_url, creds, args.timeout)
        trades = get_trades(args.clob_url, creds, args.timeout)
        trade_count, confirmed, latest_trade = trade_summary(trades)
        status = {
            "schema": "polymarket_account_readonly_v2",
            "generated_ts": generated,
            "configured": True,
            "read_only": True,
            "authenticated_execution": False,
            "open_orders": count_orders(orders),
            "trade_page_count": trade_count,
            "confirmed_trade_page_count": confirmed,
            "latest_trade_ts": latest_trade,
            "status": "healthy",
        }
        code = 0
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        status = {
            "schema": "polymarket_account_readonly_v2",
            "generated_ts": generated,
            "configured": True,
            "read_only": True,
            "authenticated_execution": False,
            "open_orders": None,
            "trade_page_count": None,
            "confirmed_trade_page_count": None,
            "latest_trade_ts": None,
            "status": "error",
            "error_type": type(error).__name__,
        }
        code = 2
    atomic_json(args.output, status)
    print(json.dumps(status, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
