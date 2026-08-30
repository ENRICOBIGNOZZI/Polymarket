from __future__ import annotations

import pathlib
import json
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_micro_taker_data as data  # noqa: E402
from v7_micro_taker_worker import (  # noqa: E402
    canonical_live_flow_features, model_validity, sample_diagnostics, sample_key,
)


def book(token: str, version: int, *, epoch: int = 1) -> data.Book:
    value = data.Book({
        "asset_id": token,
        "timestamp": 100,
        "bids": [{"price": 0.4, "size": 10}],
        "asks": [{"price": 0.5, "size": 10}],
    }, received_ts=101)
    value.state_version = version
    value.lineage_epoch = epoch
    return value


def test_sample_key_ignores_snapshot_republication() -> None:
    yes, no = book("yes", 7), book("no", 9)
    first = sample_key("market", yes, no)
    yes.snapshot_published_ts_ms += 5_000
    no.snapshot_published_ts_ms += 5_000
    assert sample_key("market", yes, no) == first
    yes.state_version += 1
    assert sample_key("market", yes, no) != first


def test_degenerate_samples_invalidate_model_instead_of_false_low_sigma() -> None:
    samples = [
        {
            "sample_key": f"m:{index}", "market_id": "m", "y": 0.0,
            "x": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        }
        for index in range(50)
    ]
    diagnostics = sample_diagnostics(samples)
    valid, reasons = model_validity(diagnostics)
    assert not valid
    assert diagnostics["effective_sample_size"] == 50
    assert diagnostics["nonzero_labeled_samples"] == 0
    assert "DEGENERATE_ZERO_TARGET_VARIANCE" in reasons
    assert "NO_CAUSAL_FLOW_COVERAGE" in reasons


def test_novel_nonzero_flow_and_targets_can_make_dataset_valid() -> None:
    samples = [
        {
            "sample_key": f"m:{index}", "market_id": f"m-{index % 5}",
            "y": 0.001 if index % 2 else -0.001,
            "x": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
        }
        for index in range(50)
    ]
    valid, reasons = model_validity(sample_diagnostics(samples))
    assert valid
    assert reasons == []


class CanonicalLiveFlowTests(unittest.TestCase):
    def test_live_flow_uses_receive_causality_and_deduplicates(self) -> None:
        now_ms = 1_000_000
        sha = "a" * 40
        trade = {
            "exchange_ts_ms": now_ms - 2_000,
            "receive_ts_ms": now_ms - 1_000,
            "side": "SELL", "price": 0.4, "size": 10.0,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "flow.json"
            path.write_text(json.dumps({
                "schema": "polymarket_v7_live_trade_flow_v1",
                "timestamp_ms": now_ms - 10,
                "producer": "FAST_STRUCTURAL_CPP_WEBSOCKET",
                "model_sha": sha,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "raw_last_trade_events": 3,
                "valid_trade_prints": 2,
                "rows": [{"token_id": "yes", "trade_prints": [trade, trade]}],
            }), encoding="utf-8")
            features, diagnostics = canonical_live_flow_features(
                path, {"yes", "no"}, model_sha=sha, now_ms=now_ms,
                lookback_seconds=60, half_life_seconds=15,
                max_publish_age_ms=2_500,
            )
        self.assertTrue(diagnostics["valid"])
        self.assertEqual(diagnostics["matched_prints"], 1)
        self.assertEqual(features["yes"]["prints"], 1.0)
        self.assertEqual(features["yes"]["signed_imbalance"], -1.0)
        self.assertEqual(features["no"]["prints"], 0.0)
