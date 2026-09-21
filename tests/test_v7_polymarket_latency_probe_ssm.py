from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))

import v7_polymarket_latency_probe_ssm as m


def test_latency_request_is_paper_only_and_bounded(tmp_path):
    request = {
        "schema": "polymarket_v7_london_latency_probe_request_v1",
        "version": 1,
        "request_id": "latency-probe-test-20260921",
        "instance_id": "i-0fba2bac9fdc5cbeb",
        "samples": 20,
        "recent_window_seconds": 900,
        "output_directory": "docs/research/london-latency-2026-09-21",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert m.load_request(path) == request


def test_latency_profiler_source_contains_no_order_mutation():
    source = (ROOT / "ops/v7_polymarket_latency_probe_ssm.py").read_text(
        encoding="utf-8")
    assert "order_path_latency_measured" in source
    assert '"order_path_latency_measured":False' in source
    assert "PUBLIC_NETWORK_HANDSHAKE_AND_NATIVE_COMPUTE_ONLY" in source
    assert "ws-subscriptions-clob.polymarket.com" in source
    assert "clob.polymarket.com" in source
    for forbidden in (
        "post_order", "create_order", "submit_order", "cancel_order",
        "real_order_submission": True", "authenticated_execution": True",
    ):
        assert forbidden not in source


def test_latency_request_rejects_real_authority(tmp_path):
    request = {
        "schema": "polymarket_v7_london_latency_probe_request_v1",
        "version": 1,
        "request_id": "latency-probe-test-20260921",
        "instance_id": "i-0fba2bac9fdc5cbeb",
        "samples": 20,
        "recent_window_seconds": 900,
        "output_directory": "docs/research/london-latency-2026-09-21",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": True,
        "real_capital_at_risk": False,
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    try:
        m.load_request(path)
    except ValueError as exc:
        assert "PAPER-only" in str(exc)
    else:
        raise AssertionError("unsafe latency request accepted")
