#!/usr/bin/env python3
"""Verified Chainlink Data Streams v3 -> canonical V7 oracle adapter.

The adapter is deliberately a data-plane process, never an execution owner.
It authenticates only to Chainlink market data, decodes the official v3 ABI
report, binds every observation to an approved feed id/decimal mapping and
writes an append-only causal tape plus an atomic current-state snapshot.

Missing credentials, malformed reports, feed mismatches, clock anomalies and
transport gaps produce ``UNKNOWN``.  External spot prices are never substituted
for the settlement oracle.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "polymarket_v7_same_oracle_event_v1"
STATUS_SCHEMA = "polymarket_v7_same_oracle_status_v1"
OFFICIAL_REST_ORIGIN = "https://api.dataengine.chain.link"
LATEST_PATH = "/api/v1/reports/latest"
_FEED_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_WORD = 32


class OracleAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class FeedBinding:
    feed_id: str
    price_decimals: int
    oracle_window_seconds: int
    resolution_source: str
    mapping_version: str
    mapping_sha: str

    def validate(self) -> None:
        if not _FEED_RE.fullmatch(self.feed_id):
            raise OracleAdapterError("binding:invalid_feed_id")
        if not 0 <= self.price_decimals <= 30:
            raise OracleAdapterError("binding:invalid_price_decimals")
        if self.oracle_window_seconds <= 0:
            raise OracleAdapterError("binding:invalid_window")
        parsed = urllib.parse.urlparse(self.resolution_source)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {
            "data.chain.link", "www.data.chain.link"
        }:
            raise OracleAdapterError("binding:resolution_source_not_chainlink")
        if not self.mapping_version.strip() or not re.fullmatch(r"[0-9a-f]{64}", self.mapping_sha):
            raise OracleAdapterError("binding:identity_missing")


@dataclass(frozen=True)
class DecodedV3:
    feed_id: str
    valid_from_timestamp: int
    observations_timestamp: int
    expires_at: int
    benchmark_price_integer: int
    bid_integer: int
    ask_integer: int


@dataclass(frozen=True)
class OracleEvent:
    schema: str
    state: str
    feed_id: str
    source_sequence: int
    connection_epoch: int
    exact_decimal: str
    value_numeric: float
    oracle_observation_ns: int
    local_receive_monotonic_ns: int
    local_receive_wall_ns: int
    valid_from_ns: int
    expires_at_ns: int
    oracle_window_seconds: int
    same_oracle_recovery: bool
    gap: bool
    healthy: bool
    mapping_version: str
    mapping_sha: str
    transport: str
    paper_only: bool = True
    authenticated_execution: bool = False
    real_order_submission: bool = False


def canonical_binding_sha(raw: dict[str, Any]) -> str:
    material = {
        "feed_id": str(raw.get("feed_id") or "").lower(),
        "price_decimals": int(raw.get("price_decimals") or 0),
        "oracle_window_seconds": int(raw.get("oracle_window_seconds") or 0),
        "resolution_source": str(raw.get("resolution_source") or "").strip(),
        "mapping_version": str(raw.get("mapping_version") or "").strip(),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_binding(path: Path) -> FeedBinding:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OracleAdapterError("binding:not_object")
    supplied_sha = str(raw.get("mapping_sha") or "").lower()
    calculated = canonical_binding_sha(raw)
    if not hmac.compare_digest(supplied_sha, calculated):
        raise OracleAdapterError("binding:sha_mismatch")
    binding = FeedBinding(
        feed_id=str(raw.get("feed_id") or "").lower(),
        price_decimals=int(raw.get("price_decimals") or 0),
        oracle_window_seconds=int(raw.get("oracle_window_seconds") or 0),
        resolution_source=str(raw.get("resolution_source") or "").strip(),
        mapping_version=str(raw.get("mapping_version") or "").strip(),
        mapping_sha=supplied_sha,
    )
    binding.validate()
    return binding


def auth_headers(api_key: str, api_secret: str, full_path: str, timestamp_ms: int) -> dict[str, str]:
    if not api_key.strip() or not api_secret:
        raise OracleAdapterError("credentials:missing")
    if not full_path.startswith("/api/v1/") or timestamp_ms <= 0:
        raise OracleAdapterError("auth:invalid_request_identity")
    string_to_sign = f"GET {full_path} {_EMPTY_SHA256} {api_key} {timestamp_ms}"
    signature = hmac.new(api_secret.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": api_key,
        "X-Authorization-Timestamp": str(timestamp_ms),
        "X-Authorization-Signature-SHA256": signature,
    }


def _uint(word: bytes, bits: int) -> int:
    if len(word) != _WORD:
        raise OracleAdapterError("abi:word_length")
    value = int.from_bytes(word, "big", signed=False)
    if value >= 1 << bits:
        raise OracleAdapterError("abi:uint_overflow")
    return value


def _int(word: bytes, bits: int) -> int:
    if len(word) != _WORD:
        raise OracleAdapterError("abi:word_length")
    unsigned = int.from_bytes(word, "big", signed=False)
    sign_mask = 1 << (bits - 1)
    low_mask = (1 << bits) - 1
    low = unsigned & low_mask
    value = low - (1 << bits) if low & sign_mask else low
    expected_prefix = ((1 << (256 - bits)) - 1) if value < 0 else 0
    if unsigned >> bits != expected_prefix:
        raise OracleAdapterError("abi:int_sign_extension")
    return value


def extract_report_blob(full_report_hex: str) -> bytes:
    text = str(full_report_hex or "")
    if text.startswith("0x"):
        text = text[2:]
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise OracleAdapterError("full_report:not_hex") from exc
    # ABI: bytes32[3], bytes reportBlob, bytes32[] rs, bytes32[] ss, bytes32 rawVs.
    # The fourth word is the dynamic offset of reportBlob.
    if len(raw) < 7 * _WORD or len(raw) % _WORD:
        raise OracleAdapterError("full_report:invalid_length")
    offset = _uint(raw[3 * _WORD:4 * _WORD], 256)
    if offset < 7 * _WORD or offset % _WORD or offset + _WORD > len(raw):
        raise OracleAdapterError("full_report:invalid_blob_offset")
    length = _uint(raw[offset:offset + _WORD], 256)
    start = offset + _WORD
    end = start + length
    if length < 9 * _WORD or end > len(raw):
        raise OracleAdapterError("full_report:truncated_blob")
    return raw[start:end]


def decode_v3_blob(blob: bytes) -> DecodedV3:
    if len(blob) != 9 * _WORD:
        raise OracleAdapterError("v3:unexpected_blob_length")
    words = [blob[i:i + _WORD] for i in range(0, len(blob), _WORD)]
    feed_id = "0x" + words[0].hex()
    # Chainlink's current official v3 ABI uses uint64 timestamps and int192 prices.
    return DecodedV3(
        feed_id=feed_id,
        valid_from_timestamp=_uint(words[1], 64),
        observations_timestamp=_uint(words[2], 64),
        expires_at=_uint(words[5], 64),
        benchmark_price_integer=_int(words[6], 192),
        bid_integer=_int(words[7], 192),
        ask_integer=_int(words[8], 192),
    )


def decode_envelope(raw: dict[str, Any], binding: FeedBinding) -> DecodedV3:
    report = raw.get("report") if isinstance(raw, dict) else None
    if not isinstance(report, dict):
        raise OracleAdapterError("envelope:report_missing")
    decoded = decode_v3_blob(extract_report_blob(str(report.get("fullReport") or "")))
    envelope_feed = str(report.get("feedID") or "").lower()
    if envelope_feed != decoded.feed_id or decoded.feed_id != binding.feed_id:
        raise OracleAdapterError("envelope:feed_mismatch")
    for key, decoded_value in (
        ("validFromTimestamp", decoded.valid_from_timestamp),
        ("observationsTimestamp", decoded.observations_timestamp),
    ):
        if key in report and int(report[key]) != decoded_value:
            raise OracleAdapterError(f"envelope:{key}_mismatch")
    return decoded


def _timestamp_ns(feed_id: str, value: int) -> int:
    # High nibble is timestamp resolution in the official feed ID contract:
    # zero=seconds, one=milliseconds. Unknown resolutions fail closed.
    resolution = int(feed_id[2], 16)
    if resolution == 0:
        multiplier = 1_000_000_000
    elif resolution == 1:
        multiplier = 1_000_000
    else:
        raise OracleAdapterError("feed:unsupported_timestamp_resolution")
    if value <= 0 or value > (2**63 - 1) // multiplier:
        raise OracleAdapterError("feed:timestamp_out_of_range")
    return value * multiplier


def make_event(
    decoded: DecodedV3,
    binding: FeedBinding,
    *,
    receive_monotonic_ns: int,
    receive_wall_ns: int,
    connection_epoch: int,
    prior_sequence: int | None,
    recovering: bool,
    max_age_ns: int,
) -> OracleEvent:
    observed_ns = _timestamp_ns(decoded.feed_id, decoded.observations_timestamp)
    valid_from_ns = _timestamp_ns(decoded.feed_id, decoded.valid_from_timestamp)
    expires_ns = _timestamp_ns(decoded.feed_id, decoded.expires_at)
    if receive_monotonic_ns <= 0 or receive_wall_ns <= 0:
        raise OracleAdapterError("receive_clock:invalid")
    if observed_ns > receive_wall_ns + 5_000_000_000:
        raise OracleAdapterError("oracle:future_observation")
    age_ns = max(0, receive_wall_ns - observed_ns)
    if age_ns > max_age_ns or decoded.benchmark_price_integer <= 0:
        raise OracleAdapterError("oracle:stale_or_nonpositive")
    if not (decoded.bid_integer > 0 and decoded.ask_integer >= decoded.bid_integer):
        raise OracleAdapterError("oracle:invalid_bid_ask")
    gap = prior_sequence is not None and decoded.observations_timestamp < prior_sequence
    duplicate = prior_sequence is not None and decoded.observations_timestamp == prior_sequence
    if gap:
        raise OracleAdapterError("oracle:sequence_regression")
    scale = 10 ** binding.price_decimals
    exact = f"{decoded.benchmark_price_integer / scale:.{binding.price_decimals}f}"
    value = decoded.benchmark_price_integer / scale
    if not math.isfinite(value) or value <= 0:
        raise OracleAdapterError("oracle:invalid_scaled_price")
    # Repeated latest snapshots are healthy but do not create a new event.
    state = "RECOVERED" if recovering else "LIVE"
    return OracleEvent(
        schema=SCHEMA,
        state=state,
        feed_id=decoded.feed_id,
        source_sequence=decoded.observations_timestamp,
        connection_epoch=connection_epoch,
        exact_decimal=exact,
        value_numeric=value,
        oracle_observation_ns=observed_ns,
        local_receive_monotonic_ns=receive_monotonic_ns,
        local_receive_wall_ns=receive_wall_ns,
        valid_from_ns=valid_from_ns,
        expires_at_ns=expires_ns,
        oracle_window_seconds=binding.oracle_window_seconds,
        same_oracle_recovery=recovering,
        gap=False,
        healthy=not duplicate or not recovering,
        mapping_version=binding.mapping_version,
        mapping_sha=binding.mapping_sha,
        transport="CHAINLINK_DATA_STREAMS_REST_V3",
    )


def fetch_latest(binding: FeedBinding, api_key: str, api_secret: str, timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode({"feedID": binding.feed_id})
    full_path = f"{LATEST_PATH}?{query}"
    now_ms = time.time_ns() // 1_000_000
    request = urllib.request.Request(
        OFFICIAL_REST_ORIGIN + full_path,
        headers={**auth_headers(api_key, api_secret, full_path, now_ms), "Accept": "application/json"},
        method="GET",
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        if response.status != 200:
            raise OracleAdapterError(f"transport:http_{response.status}")
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise OracleAdapterError("transport:response_too_large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise OracleAdapterError("transport:response_not_object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_event(path: Path, event: OracleEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def unknown_status(binding: FeedBinding | None, reason: str, epoch: int) -> dict[str, Any]:
    return {
        "schema": STATUS_SCHEMA,
        "state": "UNKNOWN",
        "reason": reason,
        "feed_id": "" if binding is None else binding.feed_id,
        "mapping_sha": "" if binding is None else binding.mapping_sha,
        "connection_epoch": epoch,
        "timestamp_ns": time.time_ns(),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def run_once(binding: FeedBinding, *, api_key: str, api_secret: str, timeout: float,
             max_age_ns: int, epoch: int, prior_sequence: int | None, recovering: bool) -> OracleEvent:
    envelope = fetch_latest(binding, api_key, api_secret, timeout)
    decoded = decode_envelope(envelope, binding)
    return make_event(
        decoded, binding,
        receive_monotonic_ns=time.monotonic_ns(),
        receive_wall_ns=time.time_ns(),
        connection_epoch=epoch,
        prior_sequence=prior_sequence,
        recovering=recovering,
        max_age_ns=max_age_ns,
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verified same-oracle Chainlink Data Streams adapter")
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--interval-ms", type=int, default=250)
    parser.add_argument("--timeout-seconds", type=float, default=2.0)
    parser.add_argument("--max-age-ms", type=int, default=2000)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        binding = load_binding(args.binding)
    except Exception as exc:
        _atomic_json(args.status, unknown_status(None, f"binding:{type(exc).__name__}:{exc}", 0))
        return 2
    api_key = os.environ.get("STREAMS_API_KEY", "")
    api_secret = os.environ.get("STREAMS_API_SECRET", "")
    if not api_key or not api_secret:
        _atomic_json(args.status, unknown_status(binding, "credentials:missing", 0))
        return 0

    prior: int | None = None
    epoch = 1
    recovering = True
    while True:
        try:
            event = run_once(
                binding, api_key=api_key, api_secret=api_secret,
                timeout=max(0.1, args.timeout_seconds), max_age_ns=max(1, args.max_age_ms) * 1_000_000,
                epoch=epoch, prior_sequence=prior, recovering=recovering,
            )
            if prior != event.source_sequence:
                _append_event(args.tape, event)
            prior = event.source_sequence
            recovering = False
            _atomic_json(args.status, asdict(event))
        except (OracleAdapterError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            _atomic_json(args.status, unknown_status(binding, f"{type(exc).__name__}:{exc}", epoch))
            epoch += 1
            recovering = True
            if args.once:
                return 2
        if args.once:
            return 0
        time.sleep(max(0.01, args.interval_ms / 1000.0))


if __name__ == "__main__":
    raise SystemExit(main())
