#!/usr/bin/env python3
"""Read-only authenticated Polymarket account probe.

Credentials are read exclusively from environment variables. The script never
prints secret material and performs GET requests only. It is safe to leave
installed while account integration is disabled.
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


def get_open_orders(base_url: str, creds: dict[str, str], timeout: float) -> Any:
    path = "/data/orders"
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
            "User-Agent": "polymarket-account-readonly/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def count_orders(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("orders", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


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
            "schema": "polymarket_account_readonly_v1",
            "generated_ts": generated,
            "configured": False,
            "read_only": True,
            "authenticated_execution": False,
            "open_orders": None,
            "status": "not_configured",
        }
        atomic_json(args.output, status)
        print(json.dumps(status, sort_keys=True))
        return 0

    try:
        payload = get_open_orders(args.clob_url, creds, args.timeout)
        status = {
            "schema": "polymarket_account_readonly_v1",
            "generated_ts": generated,
            "configured": True,
            "read_only": True,
            "authenticated_execution": False,
            "open_orders": count_orders(payload),
            "status": "healthy",
        }
        code = 0
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        status = {
            "schema": "polymarket_account_readonly_v1",
            "generated_ts": generated,
            "configured": True,
            "read_only": True,
            "authenticated_execution": False,
            "open_orders": None,
            "status": "error",
            "error_type": type(error).__name__,
        }
        code = 2
    atomic_json(args.output, status)
    print(json.dumps(status, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
