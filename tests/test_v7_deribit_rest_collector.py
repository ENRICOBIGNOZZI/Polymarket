from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_deribit_rest_collector as collector


def _meta(sequence: int) -> dict[str, int | str]:
    return {"local_receive_wall_ns": 1_800_000_000_000_000_000 + sequence,
            "local_receive_monotonic_ns": 100 + sequence,
            "request_duration_ns": 10, "raw_payload_hash": chr(97 + sequence) * 64}


def test_collect_once_discovers_future_and_surface_without_authority(monkeypatch) -> None:
    calls = iter((
        ([{"kind": "future", "is_active": True, "instrument_name": "BTC-PERPETUAL"},
          {"kind": "future", "is_active": True, "instrument_name": "BTC-30SEP26", "expiration_timestamp": 1_800_000_001_000},
          {"kind": "option", "is_active": True, "instrument_name": "BTC-30SEP26-100000-C", "expiration_timestamp": 1_800_000_001_000, "strike": 100000, "option_type": "call"}], _meta(0)),
        ([{"instrument_name": "BTC-30SEP26-100000-C", "mark_iv": 50, "mark_price": 0.1, "bid_iv": 49, "ask_iv": 51}], _meta(1)),
        ({"mark_price": 100000, "index_price": 99900, "current_funding": 0.001, "funding_8h": 0.002, "open_interest": 5}, _meta(2)),
        ({"mark_price": 100500, "open_interest": 3}, _meta(3)),
        ([[1, 55.0]], _meta(4)),
    ))
    monkeypatch.setattr(collector, "_fetch", lambda *_: next(calls))
    row, status = collector.collect_once()
    assert status["state"] == "OPERATIONAL"
    assert status["nearest_future"] == "BTC-30SEP26"
    assert row["nearest_future"]["basis_bps_to_perpetual"] == 50.0
    assert len(row["option_surface"]) == 1
    assert row["option_surface"][0]["mark_iv"] == 50.0
    assert row["polling_latency_not_event_latency"] is True
    assert row["execution_authority"] is False and row["promotion_authority"] is False


def test_surface_rejects_missing_usable_points() -> None:
    try:
        collector._option_surface([], [])
    except collector.DeribitRestError as exc:
        assert str(exc) == "option_surface_empty"
    else:
        raise AssertionError("empty option surface must be fail-closed")


def test_short_dated_surface_features_gate_sparse_quotes_and_derive_valid_metrics() -> None:
    sparse = [{"expiry_ms": 1_800_086_400_000, "strike": 100, "option_type": "call",
               "mark_iv": 50, "mark_price": .1, "bid_iv": 49, "ask_iv": 51}]
    invalid = collector._surface_features(sparse, spot=100, now_wall_ns=1_800_000_000_000_000_000)
    assert invalid["valid"] is False
    assert "MINIMUM_SHORT_DATED_QUOTE_COUNT_NOT_MET" in invalid["failure_reasons"]
    surface = []
    for expiry_ms, shift in ((1_800_086_400_000, 0), (1_800_172_800_000, 2)):
        for option_type, sign in (("call", 1), ("put", -1)):
            for strike, wing in ((90, 4), (100, 0), (110, 3)):
                surface.append({"expiry_ms": expiry_ms, "strike": strike, "option_type": option_type,
                                "mark_iv": 50 + shift + wing + (1 if option_type == "put" else 0),
                                "mark_price": .1 + (110 - strike) / 1000 if option_type == "call" else .1 + (strike - 90) / 1000,
                                "bid_iv": 49 + shift + wing, "ask_iv": 51 + shift + wing, "greeks": None})
    valid = collector._surface_features(surface, spot=100, now_wall_ns=1_800_000_000_000_000_000)
    assert valid["valid"] is True
    assert valid["atm_iv"] is not None and valid["vol_term_slope_iv_per_day"] is not None
    assert valid["interpolation_used"] is False and valid["tail_probability_available"] is False
