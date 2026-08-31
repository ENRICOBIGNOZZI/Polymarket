from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_external_forward_evidence_gate as gate


def _tape(count: int = 4) -> dict[str, object]:
    return {"enabled": True, "evidence_valid": True, "writer_healthy": True,
            "accepted": count, "written": count, "dropped": 0}


def _runtime(uptime_ns: int) -> dict[str, object]:
    event = {source: _tape() for source in gate.EVENT_SOURCES}
    raw = {source: _tape() for source in gate.RAW_SOURCES}
    valid = {"valid": True, "parse_failures": 0}
    return {"schema": "polymarket_v7_external_venue_runtime_v1", "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False, "valid": True,
            "uptime_ns": uptime_ns, "normalized_event_tapes": event, "raw_frame_tapes": raw,
            "binance_spot_l2": valid, "bybit_spot_l2": valid, "bybit_linear_l2": valid,
            "bybit_linear": valid, "deribit": valid, "binance_usdm": valid}


def test_short_clean_run_is_honestly_insufficient() -> None:
    result = gate.evaluate(_runtime(30_000_000_000),
                           {"state": "OPERATIONAL_POLLING", "hft_trigger_eligible": False},
                           {"state": "OPERATIONAL", "option_surface_valid": True}, min_duration_s=60.0)
    assert result["engineering_valid"] is True
    assert result["state"] == "FORWARD_EVIDENCE_INSUFFICIENT"
    assert result["forward_evidence_sufficient"] is False


def test_duration_and_durable_tapes_are_required() -> None:
    result = gate.evaluate(_runtime(61_000_000_000),
                           {"state": "OPERATIONAL_POLLING", "hft_trigger_eligible": False},
                           {"state": "OPERATIONAL", "option_surface_valid": True}, min_duration_s=60.0)
    assert result["state"] == "ENGINEERING_VALIDATED"
    broken = _runtime(61_000_000_000)
    broken["raw_frame_tapes"]["bybit_spot"]["dropped"] = 1  # type: ignore[index]
    failed = gate.evaluate(broken, {"state": "OPERATIONAL_POLLING", "hft_trigger_eligible": False},
                           {"state": "OPERATIONAL", "option_surface_valid": True}, min_duration_s=60.0)
    assert failed["engineering_valid"] is False
    assert "TAPE_INCOMPLETE:bybit_spot" in failed["failures"]


def test_active_recorder_inflight_write_does_not_create_a_false_failure() -> None:
    runtime = _runtime(61_000_000_000)
    runtime["normalized_event_tapes"]["bybit_linear"]["written"] = 3  # type: ignore[index]
    result = gate.evaluate(runtime, {"state": "OPERATIONAL_POLLING", "hft_trigger_eligible": False},
                           {"state": "OPERATIONAL", "option_surface_valid": True}, min_duration_s=60.0)
    assert result["engineering_valid"] is True
    assert result["state"] == "ENGINEERING_VALIDATED"


def test_binance_usdm_market_normalized_tape_and_health_are_required() -> None:
    broken = _runtime(61_000_000_000)
    broken["normalized_event_tapes"].pop("binance_usdm_market")  # type: ignore[index]
    broken["binance_usdm"]["valid"] = False  # type: ignore[index]
    failed = gate.evaluate(broken, {"state": "OPERATIONAL_POLLING", "hft_trigger_eligible": False},
                           {"state": "OPERATIONAL", "option_surface_valid": True}, min_duration_s=60.0)
    assert failed["engineering_valid"] is False
    assert "TAPE_MISSING:binance_usdm_market" in failed["failures"]
    assert "LIVE_SOURCE_UNHEALTHY:binance_usdm" in failed["failures"]
