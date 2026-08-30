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
    canonical_live_flow_features, chronological_oos_diagnostics, model_validity,
    sample_diagnostics, sample_key,
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
            "event_id": f"e-{index % 5}", "ts": 1_000 + index * 30,
            "y": 0.001 if index % 2 else -0.001,
            "x": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
        }
        for index in range(240)
    ]
    valid, reasons = model_validity(sample_diagnostics(samples))
    assert valid
    assert reasons == []


def test_correlated_burst_cannot_unlock_capital_from_raw_row_count() -> None:
    samples = [
        {
            "sample_key": f"m:{index}", "market_id": f"m-{index % 20}",
            "event_id": f"e-{index % 20}", "ts": 1_000,
            "y": 0.001 if index % 2 else -0.001,
            "x": [1.0, 0.1, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
        }
        for index in range(500)
    ]
    valid, reasons = model_validity(sample_diagnostics(samples))
    assert not valid
    assert "INSUFFICIENT_INDEPENDENT_TIME_BUCKETS" in reasons


def test_purged_chronological_oos_requires_real_predictive_skill() -> None:
    samples = []
    for index in range(400):
        signal = -1.0 if index % 2 else 1.0
        samples.append({
            "sample_key": f"m:{index}", "market_id": f"m-{index % 8}",
            "event_id": f"e-{index % 8}", "ts": 1_000 + index * 2,
            "y": 0.002 * signal,
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
        })
    diagnostics = chronological_oos_diagnostics(samples, horizon_seconds=30)
    assert diagnostics["valid"]
    assert diagnostics["mse_improvement_fraction"] > 0.90
    assert diagnostics["prediction_target_correlation"] > 0.90
    assert diagnostics["directional_accuracy"] > 0.90


def test_purged_chronological_oos_rejects_no_skill() -> None:
    samples = []
    for index in range(400):
        signal = -1.0 if index % 2 else 1.0
        target = 0.002 if (index // 2) % 2 else -0.002
        samples.append({
            "sample_key": f"m:{index}", "market_id": f"m-{index % 8}",
            "event_id": f"e-{index % 8}", "ts": 1_000 + index * 2,
            "y": target,
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
        })
    diagnostics = chronological_oos_diagnostics(samples, horizon_seconds=30)
    assert not diagnostics["valid"]
    assert "OOS_DOES_NOT_BEAT_NO_CHANGE_BASELINE" in diagnostics["reasons"]


class DatasetReadinessTests(unittest.TestCase):
    # Keep the plain functions convenient for lightweight direct invocation,
    # while making them part of the repository's canonical unittest discovery.
    def test_sample_key_novelty(self) -> None:
        test_sample_key_ignores_snapshot_republication()

    def test_degenerate_model_gate(self) -> None:
        test_degenerate_samples_invalidate_model_instead_of_false_low_sigma()

    def test_dataset_validity(self) -> None:
        test_novel_nonzero_flow_and_targets_can_make_dataset_valid()

    def test_correlated_burst_gate(self) -> None:
        test_correlated_burst_cannot_unlock_capital_from_raw_row_count()

    def test_predictive_oos_gate(self) -> None:
        test_purged_chronological_oos_requires_real_predictive_skill()

    def test_no_skill_oos_gate(self) -> None:
        test_purged_chronological_oos_rejects_no_skill()


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
