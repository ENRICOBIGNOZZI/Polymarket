from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_coinbase_l2_rest_collector as collector


def test_polling_snapshot_is_explicitly_non_realtime_and_non_executing(monkeypatch) -> None:
    payload = {
        "sequence": 42, "time": "2026-09-01T00:00:00Z",
        "bids": [["100", "3", 2], ["99", "1", 1]],
        "asks": [["101", "4", 2], ["102", "1", 1]],
    }
    request = {"local_receive_wall_ns": 1000, "local_receive_monotonic_ns": 100,
               "request_duration_ns": 10, "raw_payload_hash": "a" * 64, "raw_payload_bytes": 10}
    monkeypatch.setattr(collector, "_fetch", lambda *_: (payload, request))
    row, status = collector.collect_once()
    assert status["state"] == "OPERATIONAL_POLLING"
    assert row["best_bid"] == 100.0 and row["best_ask"] == 101.0
    assert row["bid_depth_l20"] == 4.0 and row["ask_depth_l20"] == 5.0
    assert row["realtime_l2_continuity"] is False and row["hft_trigger_eligible"] is False
    assert row["execution_authority"] is False and row["promotion_authority"] is False


def test_crossed_or_unordered_snapshots_fail_closed(monkeypatch) -> None:
    request = {"local_receive_wall_ns": 1000, "local_receive_monotonic_ns": 100,
               "request_duration_ns": 10, "raw_payload_hash": "a" * 64, "raw_payload_bytes": 10}
    payload = {"sequence": 42, "bids": [["100", "1"]], "asks": [["99", "1"]]}
    monkeypatch.setattr(collector, "_fetch", lambda *_: (payload, request))
    try:
        collector.collect_once()
    except collector.CoinbaseL2RestError as exc:
        assert str(exc) == "crossed_book"
    else:
        raise AssertionError("crossed REST snapshot must not be accepted")
