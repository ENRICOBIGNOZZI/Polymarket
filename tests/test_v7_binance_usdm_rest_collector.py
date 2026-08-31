from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_binance_usdm_rest_collector as collector


def test_collect_once_labels_rest_and_never_grants_authority(monkeypatch) -> None:
    responses = iter((
        ({"markPrice": "100", "indexPrice": "99.5", "lastFundingRate": "0.0001", "nextFundingTime": 10},
         {"local_receive_wall_ns": 100, "local_receive_monotonic_ns": 100, "request_duration_ns": 10, "raw_payload_hash": "a" * 64}),
        ({"openInterest": "42"},
         {"local_receive_wall_ns": 101, "local_receive_monotonic_ns": 101, "request_duration_ns": 10, "raw_payload_hash": "b" * 64}),
    ))
    monkeypatch.setattr(collector, "_fetch", lambda *_: next(responses))
    row, status = collector.collect_once()
    assert status["state"] == "OPERATIONAL"
    assert row["transport"] == "PUBLIC_REST_POLLING"
    assert row["polling_latency_not_event_latency"] is True
    assert row["execution_authority"] is False
    assert row["mark_index_basis_bps"] > 0
