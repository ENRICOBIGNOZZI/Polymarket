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
