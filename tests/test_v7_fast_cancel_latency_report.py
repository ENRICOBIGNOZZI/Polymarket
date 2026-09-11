from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_fast_cancel_latency_report as latency  # noqa: E402


def row(trigger: int, decision: int, recorded_ms: int, external: bool = True):
    envelope = {
        "action": "CANCEL",
        "reasons": ["RESEARCH_CANCEL_RULE_MATCH"] if external else ["OTHER"],
        "source_event_timestamps_ns": [trigger],
        "decision_receive_timestamp_ns": decision,
        "deterministic_replay_key": "r",
    }
    return {
        "record_id": str(recorded_ms),
        "event_type": "ORDER_STATE",
        "order_state": "CANCEL_REQUESTED",
        "recorded_ts_ms": recorded_ms,
        "market_id": "m",
        "event_id": "e",
        "metadata": {"external_cancel_opportunity_envelope": envelope},
    }


def test_realized_external_cancel_latency_report() -> None:
    report = latency.build_report([
        row(1_000_000_000, 1_005_000_000, 1020),
        row(2_000_000_000, 2_010_000_000, 2050),
    ])
    assert report["diagnostics"]["usable_external_cancel_rows"] == 2
    assert report["trigger_to_cancel_request"]["p99_ms"] < 51.0
    assert report["decision_to_cancel_request"]["max_ms"] < 41.0
    assert report["trigger_to_cancel_request"]["fraction_le_100ms"] == 1.0


def test_non_external_cancel_is_excluded() -> None:
    report = latency.build_report([row(1_000_000_000, 1_005_000_000, 1020, False)])
    assert report["diagnostics"]["usable_external_cancel_rows"] == 0
    assert report["diagnostics"]["non_external_cancel_rows"] == 1


def test_invalid_timestamp_order_is_excluded() -> None:
    report = latency.build_report([row(2_000_000_000, 1_900_000_000, 2050)])
    assert report["diagnostics"]["usable_external_cancel_rows"] == 0
    assert report["diagnostics"]["invalid_timestamp_order"] == 1


if __name__ == "__main__":
    test_realized_external_cancel_latency_report()
    test_non_external_cancel_is_excluded()
    test_invalid_timestamp_order_is_excluded()
