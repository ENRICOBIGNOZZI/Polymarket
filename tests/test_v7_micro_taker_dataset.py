from __future__ import annotations

import pathlib
import copy
import json
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_micro_taker_data as data  # noqa: E402
from v7_micro_target import TARGET_SEMANTICS_VERSION  # noqa: E402
from v7_micro_taker_worker import (  # noqa: E402
    MODEL_FEATURE_DIM, MODEL_SPEC_VERSION, canonical_live_flow_features,
    causal_model_rows,
    chronological_oos_diagnostics, executable_strategy_oos,
    fixed_forward_oos_diagnostics,
    load_or_freeze_model_challenger, model_prediction, model_validity,
    compact_samples, sample_diagnostics, sample_key,
)


def executable_labels(target: float, *, stressed: float | None = None) -> dict:
    magnitude = abs(target)
    stress = magnitude if stressed is None else stressed
    return {
        "yes_executable_net_edge": magnitude if target > 0.0 else -magnitude,
        "no_executable_net_edge": magnitude if target < 0.0 else -magnitude,
        "yes_cost_stress_2x_net_edge": stress if target > 0.0 else -magnitude,
        "no_cost_stress_2x_net_edge": stress if target < 0.0 else -magnitude,
        "label_probe_shares": 5.0,
    }


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
            "yes_executable_net_edge": 0.0,
            "no_executable_net_edge": 0.0,
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
            **executable_labels(0.001 if index % 2 else -0.001),
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
            **executable_labels(0.001 if index % 2 else -0.001),
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
            **executable_labels(0.002 * signal),
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
        })
    diagnostics = chronological_oos_diagnostics(samples, horizon_seconds=30)
    assert diagnostics["valid"]
    assert diagnostics["mse_improvement_fraction"] > 0.02
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
            **executable_labels(target),
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
        })
    diagnostics = chronological_oos_diagnostics(samples, horizon_seconds=30)
    assert not diagnostics["valid"]
    assert any("EXECUTABLE" in reason or "FOLD_STABILITY" in reason
               for reason in diagnostics["reasons"])


def test_executable_oos_equal_weights_event_time_and_requires_2x_cost_profit() -> None:
    rows = []
    predictions = []
    for index in range(60):
        target = 0.01
        row = {
            "market_id": f"m-{index}", "event_id": f"e-{index}",
            "ts": 1_000 + 31 * index, "y": target,
            **executable_labels(target, stressed=-0.001),
        }
        rows.append(row)
        predictions.append({"YES": 0.01, "NO": -0.01,
                            "side": "YES", "edge": 0.01})
    diagnostics = executable_strategy_oos(
        rows, predictions, horizon_seconds=30)
    assert diagnostics["mean_executable_net_edge"] > 0.0
    assert diagnostics["lower_95_executable_net_edge"] > 0.0
    assert diagnostics["lower_95_2x_cost_net_edge"] < 0.0
    assert not diagnostics["valid"]
    assert "EXECUTABLE_2X_COST_EDGE_CI_NOT_POSITIVE" in diagnostics["reasons"]

    burst_rows = [{**rows[0], "market_id": f"copy-{index}"} for index in range(100)]
    burst = executable_strategy_oos(
        burst_rows, [predictions[0]] * len(burst_rows), horizon_seconds=30)
    assert burst["event_time_clusters"] == 1
    assert "INSUFFICIENT_EXECUTABLE_EVENT_TIME_CLUSTERS" in burst["reasons"]


def test_lag_features_never_borrow_same_second_observations() -> None:
    samples = [
        {"sample_key": "a", "market_id": "m", "ts": 1_000,
         "mid": 0.40, "spread": 0.02, "x": [1.0] + [0.0] * 7},
        {"sample_key": "b", "market_id": "m", "ts": 1_000,
         "mid": 0.45, "spread": 0.02, "x": [1.0] + [0.0] * 7},
        {"sample_key": "c", "market_id": "m", "ts": 1_002,
         "mid": 0.50, "spread": 0.02, "x": [1.0] + [0.0] * 7},
    ]
    rows = causal_model_rows(samples)
    assert rows[0]["x"][-4:] == [0.0, 0.0, 0.0, 0.0]
    assert rows[1]["x"][-4:] == [0.0, 0.0, 0.0, 0.0]
    assert abs(rows[2]["x"][-4] - 0.05) < 1e-12


def test_frozen_challenger_uses_only_post_selection_forward_rows() -> None:
    rows = []
    for index in range(100):
        signal = -1.0 if index % 2 else 1.0
        rows.append({
            "sample_key": f"train:{index}", "market_id": f"m-{index % 15}",
            "event_id": f"e-{index % 15}", "ts": 1_000 + index * 2,
            "mid": 0.5, "spread": 0.01,
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
            "y": 0.002 * signal,
            **executable_labels(0.002 * signal),
        })
    model_rows = causal_model_rows(rows)
    challenger, readiness = load_or_freeze_model_challenger(
        {}, model_rows, horizon_seconds=30, selected_at_ts=9_999)
    assert readiness["ready"]
    assert challenger is not None
    boundary = int(challenger["validation_start_ts"])
    yes_beta = list(challenger["yes_beta"])
    no_beta = list(challenger["no_beta"])
    threshold = float(challenger["prediction_activity_threshold"])
    pre_only = fixed_forward_oos_diagnostics(
        model_rows, yes_beta=yes_beta, no_beta=no_beta, threshold=threshold,
        validation_start_ts=boundary, horizon_seconds=30)
    assert pre_only["oos_samples"] == 0
    assert "INSUFFICIENT_FORWARD_OOS_SAMPLES" in pre_only["reasons"]

    future = []
    for index in range(60):
        signal = -1.0 if index % 2 else 1.0
        future.append({
            "sample_key": f"future:{index}",
            "market_id": f"future-m-{index % 15}",
            "event_id": f"future-e-{index % 15}",
            "ts": boundary + 31 + index * 31,
            "mid": 0.5, "spread": 0.01,
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
            "y": 0.002 * signal,
            **executable_labels(0.002 * signal),
        })
    combined = causal_model_rows(rows + future)
    forward = fixed_forward_oos_diagnostics(
        combined, yes_beta=yes_beta, no_beta=no_beta, threshold=threshold,
        validation_start_ts=boundary, horizon_seconds=30)
    assert forward["oos_samples"] == 60
    assert forward["independent_time_buckets"] >= 12
    assert forward["unique_events"] >= 12
    assert forward["valid"]
    assert len(yes_beta) == len(no_beta) == MODEL_FEATURE_DIM


def test_dual_heads_learn_dense_edges_when_sparse_action_target_is_zero() -> None:
    rows = []
    for index in range(160):
        signal = -1.0 if index % 2 else 1.0
        rows.append({
            "sample_key": f"dense:{index}",
            "market_id": f"m-{index % 20}",
            "event_id": f"e-{index % 20}",
            "ts": 1_000 + index * 2,
            "mid": 0.5, "spread": 0.01,
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
            # The old sparse target discarded every one of these observations.
            "y": 0.0,
            "yes_executable_net_edge": -0.010 + 0.003 * signal,
            "no_executable_net_edge": -0.012 - 0.002 * signal,
            "yes_cost_stress_2x_net_edge": -0.012 + 0.003 * signal,
            "no_cost_stress_2x_net_edge": -0.014 - 0.002 * signal,
            "label_probe_shares": 5.0,
        })
    diagnostics = sample_diagnostics(rows)
    assert diagnostics["labeled_samples"] == len(rows)
    assert diagnostics["nonzero_labeled_samples"] == len(rows)
    assert diagnostics["target_variance"] > 0.0

    model_rows = causal_model_rows(rows)
    challenger, readiness = load_or_freeze_model_challenger(
        {}, model_rows, horizon_seconds=30, selected_at_ts=9_999)
    assert readiness["ready"]
    assert challenger is not None
    prediction = model_prediction(
        model_rows[-1]["x"], challenger["yes_beta"], challenger["no_beta"], 0.0)
    assert abs(prediction["YES"]) > 1e-6
    assert abs(prediction["NO"]) > 1e-6
    assert prediction["side"] is None
    assert prediction["edge"] == 0.0


def test_dual_prediction_selects_best_positive_side_only() -> None:
    x = [1.0] + [0.0] * (MODEL_FEATURE_DIM - 1)
    yes = [0.020] + [0.0] * (MODEL_FEATURE_DIM - 1)
    no = [0.010] + [0.0] * (MODEL_FEATURE_DIM - 1)
    prediction = model_prediction(x, yes, no, 0.0)
    assert prediction["side"] == "YES"
    assert prediction["edge"] > 0.0

    both_negative = model_prediction(
        x,
        [-0.010] + [0.0] * (MODEL_FEATURE_DIM - 1),
        [-0.020] + [0.0] * (MODEL_FEATURE_DIM - 1),
        0.0,
    )
    assert both_negative["side"] is None
    assert both_negative["edge"] == 0.0


def test_legacy_single_head_challenger_is_never_loaded() -> None:
    rows = []
    for index in range(100):
        signal = -1.0 if index % 2 else 1.0
        rows.append({
            "sample_key": f"legacy:{index}", "market_id": f"m-{index % 15}",
            "event_id": f"e-{index % 15}", "ts": 1_000 + index * 2,
            "mid": 0.5, "spread": 0.01,
            "x": [1.0, signal, 0.0, 0.0, 0.0, 0.0, 0.2, -0.1],
            "y": 0.0,
            **executable_labels(0.002 * signal),
        })
    stale = {"model_challenger": {
        "model_spec_version": "executable_net_edge_sparse_ridge_v4",
        "feature_dimension": MODEL_FEATURE_DIM,
        "beta": [99.0] * MODEL_FEATURE_DIM,
        "prediction_activity_threshold": 0.0,
        "validation_start_ts": 1,
    }}
    challenger, readiness = load_or_freeze_model_challenger(
        stale, causal_model_rows(rows), horizon_seconds=30, selected_at_ts=9_999)
    assert readiness["reason"] == "CHALLENGER_FROZEN"
    assert challenger is not None
    assert challenger["model_spec_version"] == MODEL_SPEC_VERSION
    assert "beta" not in challenger


def _storage_sample(key: str, ts: int, *, labeled: bool = False) -> dict:
    row = {
        "sample_key": key,
        "market_id": "market",
        "event_id": "event",
        "ts": ts,
        "target_semantics_version": TARGET_SEMANTICS_VERSION,
        "label_horizon_seconds": 30,
        "label_probe_shares": 5.0,
        "round_trip_slippage_bps": 5.0,
        "adverse_markout_bps": 2.0,
        "capital_cost_bps_per_hour": 0.25,
        "fee": {"authoritative": True, "rate": 0.0, "exponent": 1.0},
        "yes_bids": [[0.49, 2.0], [0.48, 3.0], [0.47, 100.0]],
        "yes_asks": [[0.51, 2.0], [0.52, 3.0], [0.53, 100.0]],
        "no_bids": [[0.49, 2.0], [0.48, 3.0], [0.47, 100.0]],
        "no_asks": [[0.51, 2.0], [0.52, 3.0], [0.53, 100.0]],
        "mid": 0.5,
        "spread": 0.02,
        "x": [1.0] + [0.0] * 7,
        "y": None,
    }
    if labeled:
        row.update(executable_labels(0.001))
        row["y"] = 0.001
    return row


def test_storage_compaction_drops_only_irrecoverable_origins_and_old_payloads() -> None:
    samples = [
        _storage_sample("expired", 900),
        _storage_sample("old-labeled", 900, labeled=True),
        _storage_sample("recent", 990),
    ]
    compacted, diagnostics = compact_samples(
        samples, now=1_000, horizon_seconds=30,
        max_target_staleness_seconds=10,
    )
    by_key = {row["sample_key"]: row for row in compacted}
    assert set(by_key) == {"old-labeled", "recent"}
    assert all(key not in by_key["old-labeled"] for key in (
        "yes_bids", "yes_asks", "no_bids", "no_asks"))
    assert by_key["recent"]["yes_bids"] == [[0.49, 2.0], [0.48, 3.0]]
    assert by_key["recent"]["yes_asks"] == [[0.51, 2.0], [0.52, 3.0]]
    assert diagnostics["dropped_irrecoverable_unlabeled_samples"] == 1
    assert diagnostics["stripped_mature_labeled_book_payloads"] == 1
    assert diagnostics["book_levels_after"] < diagnostics["book_levels_before"]


def test_probe_depth_compaction_preserves_exact_target_economics() -> None:
    origin = _storage_sample("origin", 100)
    target = _storage_sample("target", 129)
    full = [copy.deepcopy(origin), copy.deepcopy(target)]
    compacted, _ = compact_samples(
        [copy.deepcopy(origin), copy.deepcopy(target)], now=129,
        horizon_seconds=30, max_target_staleness_seconds=10,
    )
    full_stats = data.label_matured_samples(
        full, now=130, horizon_seconds=30, max_target_staleness_seconds=10)
    compacted_stats = data.label_matured_samples(
        compacted, now=130, horizon_seconds=30,
        max_target_staleness_seconds=10,
    )
    assert full_stats["newly_labeled"] == 1
    assert compacted_stats["newly_labeled"] == 1
    full_origin = next(row for row in full if row["sample_key"] == "origin")
    compact_origin = next(
        row for row in compacted if row["sample_key"] == "origin")
    for key in (
        "yes_executable_net_edge", "no_executable_net_edge",
        "yes_cost_stress_2x_net_edge", "no_cost_stress_2x_net_edge",
    ):
        assert abs(full_origin[key] - compact_origin[key]) < 1e-15


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

    def test_grouped_executable_profit_gate(self) -> None:
        test_executable_oos_equal_weights_event_time_and_requires_2x_cost_profit()

    def test_strictly_prior_lag_features(self) -> None:
        test_lag_features_never_borrow_same_second_observations()

    def test_frozen_forward_challenger(self) -> None:
        test_frozen_challenger_uses_only_post_selection_forward_rows()

    def test_dense_dual_targets(self) -> None:
        test_dual_heads_learn_dense_edges_when_sparse_action_target_is_zero()

    def test_dual_side_selection(self) -> None:
        test_dual_prediction_selects_best_positive_side_only()

    def test_legacy_challenger_rejected(self) -> None:
        test_legacy_single_head_challenger_is_never_loaded()

    def test_storage_compaction(self) -> None:
        test_storage_compaction_drops_only_irrecoverable_origins_and_old_payloads()

    def test_storage_target_identity(self) -> None:
        test_probe_depth_compaction_preserves_exact_target_economics()


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
