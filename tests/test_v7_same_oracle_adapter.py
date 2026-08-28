#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oracle", ROOT / "scripts/v7_same_oracle_adapter.py")
oracle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = oracle
spec.loader.exec_module(oracle)


def word(value: int, *, signed: bool = False, bits: int = 256) -> bytes:
    if signed and value < 0:
        value = (1 << bits) + value
        value |= ((1 << (256 - bits)) - 1) << bits
    return int(value).to_bytes(32, "big")


def full_report(feed: str, price: int, ts: int = 1_800_000_000) -> str:
    blob = b"".join([
        bytes.fromhex(feed[2:]), word(ts), word(ts), word(1), word(1), word(ts + 10),
        word(price, signed=True, bits=192), word(price - 100, signed=True, bits=192),
        word(price + 100, signed=True, bits=192),
    ])
    # Valid outer ABI with empty signature arrays. Only reportBlob is consumed.
    head = b"".join([b"\0" * 32] * 3 + [word(224), word(224 + 32 + len(blob)),
                                               word(224 + 32 + len(blob) + 32), b"\0" * 32])
    return "0x" + (head + word(len(blob)) + blob + word(0) + word(0)).hex()


def binding(feed: str) -> oracle.FeedBinding:
    raw = {
        "feed_id": feed, "price_decimals": 8, "oracle_window_seconds": 60,
        "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "mapping_version": "btc-twap-60s-v1",
    }
    raw["mapping_sha"] = oracle.canonical_binding_sha(raw)
    return oracle.FeedBinding(**raw)


def test_auth_matches_official_string_contract() -> None:
    path = "/api/v1/reports/latest?feedID=0xabc"
    got = oracle.auth_headers("key", "secret", path, 123)
    body_hash = hashlib.sha256(b"").hexdigest()
    expected = hmac.new(b"secret", f"GET {path} {body_hash} key 123".encode(), hashlib.sha256).hexdigest()
    assert got["X-Authorization-Signature-SHA256"] == expected


def test_v3_full_report_decode_and_exact_binding() -> None:
    feed = "0x0003" + "11" * 30
    b = binding(feed)
    raw = {"report": {"feedID": feed, "validFromTimestamp": 1_800_000_000,
                       "observationsTimestamp": 1_800_000_000,
                       "fullReport": full_report(feed, 6_500_012_500_000)}}
    decoded = oracle.decode_envelope(raw, b)
    assert decoded.benchmark_price_integer == 6_500_012_500_000
    event = oracle.make_event(decoded, b, receive_monotonic_ns=5,
                              receive_wall_ns=1_800_000_000_100_000_000,
                              connection_epoch=1, prior_sequence=None, recovering=True,
                              max_age_ns=2_000_000_000)
    assert event.state == "RECOVERED" and event.same_oracle_recovery
    assert event.value_numeric == 65000.125
    assert event.exact_decimal == "65000.12500000"
    assert event.paper_only and not event.authenticated_execution and not event.real_order_submission


def test_feed_mismatch_and_bad_binding_fail_closed() -> None:
    feed = "0x0003" + "22" * 30
    other = "0x0003" + "33" * 30
    raw = {"report": {"feedID": other, "fullReport": full_report(other, 1_000_000_000)}}
    try:
        oracle.decode_envelope(raw, binding(feed))
    except oracle.OracleAdapterError as exc:
        assert "feed_mismatch" in str(exc)
    else:
        raise AssertionError("mismatched oracle feed accepted")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "binding.json"
        p.write_text(json.dumps({
            "feed_id": feed, "price_decimals": 8, "oracle_window_seconds": 60,
            "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "mapping_version": "v1", "mapping_sha": "0" * 64,
        }))
        try:
            oracle.load_binding(p)
        except oracle.OracleAdapterError as exc:
            assert "sha_mismatch" in str(exc)
        else:
            raise AssertionError("mutated binding accepted")


if __name__ == "__main__":
    test_auth_matches_official_string_contract()
    test_v3_full_report_decode_and_exact_binding()
    test_feed_mismatch_and_bad_binding_fail_closed()
