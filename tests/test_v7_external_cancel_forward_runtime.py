from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_external_cancel_forward_runtime as runtime  # noqa: E402

SHA = "a" * 40
REGISTRY = ROOT / "config/v7_maker_fillability_experiments.json"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_protocol_is_exactly_frozen_and_receive_time_gridded() -> None:
    exp = runtime.experiment(REGISTRY)
    pins = runtime.official_v3_evidence(ROOT / "config/v7_crypto_execution_alpha.json")
    protocol = runtime.build_protocol(
        exp, maker_model_sha=SHA, maker_model_published_ms=1_000,
        promotion_boundary_ms=int(pins["promotion_boundary_ms"]),
    )
    rule = exp["frozen_rule"]
    assert protocol["canonical_rule_sha256"] == runtime.sha256_json(rule)
    assert protocol["canonical_rule"] == rule
    assert protocol["promotion_evidence_market_started_strictly_after_ms"] == 1788781327887
    trigger = protocol["trigger_protocol"]
    assert trigger["grid_ms"] == 25
    assert trigger["shock_window_ms"] == 100
    assert trigger["cooldown_ms"] == 250
    assert trigger["minimum_absolute_log_return_bp"] == 0.3
    development = protocol["development_semantics"]
    assert development["overlap_warmup_ms"] == 300
    assert development["overlap_tail_ms"] == 1200
    assert development["no_future_snapshot_requirement"] is True
    assert protocol["overlay"]["effective_cancel_latency_ms"] == 100
    assert protocol["overlay"]["cancel_only_stale_side"] is True
    assert protocol["stress"]["queue_ahead_multiplier"] == 3.0
    assert protocol["stress"]["effective_cancel_latency_ms"] == 200
    assert protocol["labels"]["horizons_ms"] == [250, 500, 1000]


def _baseline_report(exp: dict) -> dict:
    return {
        "schema": "polymarket_v7_btc_m5_external_cancel_forward_report_v3",
        "experiment_id": runtime.EXPERIMENT_ID, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "real_money_authority": False, "automatic_promotion": False,
        "state": "PASS", "freeze_merge_sha": exp["freeze_merge_sha"],
        "freeze_timestamp_utc": exp["freeze_merge_timestamp_utc"],
        "freeze_boundary_ms": runtime.freeze_ms(exp),
        "rule_sha256": runtime.sha256_json(exp["frozen_rule"]),
        "market_count": 35, "market_count_with_avoidable_fills": 34,
        "episode_count": 300, "avoidable_fill_events": 60,
        "avoidable_filled_shares": 250.0, "stress_avoidable_fill_events": 40,
        "stress_avoidable_filled_shares": 180.0,
        "minimum_markets": 30, "minimum_avoidable_fill_events": 50,
        "equal_weight_500ms_improvement_per_share": 0.02,
        "leave_best_market_out_500ms_improvement_per_share": 0.015,
        "positive_market_fraction": 0.80,
        "stress_3x_queue_200ms_cancel_improvement_per_share": 0.01,
        "bootstrap95_market_cluster_500ms_improvement": [0.005, 0.03],
        "reason_codes": [], "markets": [],
    }


def test_immutable_baseline_requires_hash_pinned_protocol_report_and_manifest() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); exp = runtime.experiment(REGISTRY)
        protocol_path = root / "protocol.json"
        report_path = root / "report.json"
        manifest_path = root / "manifest.json"
        protocol = runtime.build_protocol(
            exp, maker_model_sha=SHA, maker_model_published_ms=1_000,
            promotion_boundary_ms=1788781327887,
        )
        write(protocol_path, protocol)
        write(report_path, _baseline_report(exp))
        protocol_sha = runtime.sha256_file(protocol_path)
        report_sha = runtime.sha256_file(report_path)
        write(manifest_path, {
            "schema": runtime.BASELINE_MANIFEST_SCHEMA, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "automatic_promotion": False, "market_count": 35,
            "protocol": {"path": str(protocol_path), "sha256": protocol_sha},
            "report": {"path": str(report_path), "sha256": report_sha},
            "episode_files": [], "external_segments": [],
        })
        pins = {
            "promotion_boundary_ms": 1788781327887,
            "protocol_sha256": protocol_sha,
            "baseline_report_sha256": report_sha,
            "baseline_manifest_sha256": runtime.sha256_file(manifest_path),
            "rule_sha256": runtime.sha256_json(exp["frozen_rule"]),
            "freeze_merge_sha": exp["freeze_merge_sha"],
            "minimum_independent_markets": 30,
        }
        report, provenance = runtime.verify_immutable_baseline(
            report_path=report_path, manifest_path=manifest_path,
            protocol_path=protocol_path, pins=pins, exp=exp,
        )
        assert report is not None and report["state"] == "PASS"
        assert provenance["verified"] is True and provenance["market_count"] == 35
        report_path.write_text(report_path.read_text() + " ", encoding="utf-8")
        try:
            runtime.verify_immutable_baseline(
                report_path=report_path, manifest_path=manifest_path,
                protocol_path=protocol_path, pins=pins, exp=exp,
            )
        except ValueError as exc:
            assert str(exc) == "official_v3_baseline_hash_mismatch"
        else:
            raise AssertionError("mutated baseline report accepted")


def test_external_segments_include_closed_gzip_but_never_open_files() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); events = root / "external_fair/normalized_events"
        events.mkdir(parents=True)
        for name in (
            "binance-spot.a.bin", "binance-spot.b.bin.gz", "coinbase-spot.a.bin.gz",
            "coinbase-spot.live.bin.open",
        ):
            (events / name).write_bytes(b"x")
        names = [path.name for path in runtime.external_segments(root)]
        assert names == ["binance-spot.a.bin", "binance-spot.b.bin.gz", "coinbase-spot.a.bin.gz"]


def test_no_closed_markets_materializes_explicit_ineligible_activation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run_root = root / "run"
        research_root = root / "durable"
        write(run_root / "micro_maker/execution_model.json", {
            "schema": "polymarket_v7_maker_execution_model_v1",
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "model_sha": SHA,
            "generated_ts_ms": 1_000,
        })
        status = runtime.evaluate_once(
            run_root=run_root, research_root=research_root,
            registry=REGISTRY, tape_dump=root / "missing-dump", model_sha=SHA,
        )
        assert status["forward_state"] == "FORWARD_EVIDENCE_INSUFFICIENT"
        assert status["market_count"] == 0
        assert status["avoidable_fill_events"] == 0
        assert status["activation_eligible"] is False
        activation = json.loads((run_root / "control/external_cancel_activation.json").read_text())
        assert activation["paper_execution_alpha_overlay_eligible"] is False
        assert activation["forward_state"] == "FORWARD_EVIDENCE_INSUFFICIENT"


def test_runtime_failure_overwrites_any_prior_true_activation() -> None:
    import subprocess
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run_root = root / "run"
        research_root = root / "durable"
        write(run_root / "control/external_cancel_activation.json", {
            "paper_execution_alpha_overlay_eligible": True,
            "schema": "stale_true_fixture",
        })
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts/v7_external_cancel_forward_runtime.py"),
            "--run-root", str(run_root), "--research-root", str(research_root),
            "--registry", str(REGISTRY), "--tape-dump", str(root / "missing-dump"),
            "--model-sha", SHA,
        ], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert result.returncode == 0
        activation = json.loads(
            (run_root / "control/external_cancel_activation.json").read_text()
        )
        assert activation["paper_execution_alpha_overlay_eligible"] is False
        assert activation["forward_state"] == "RUNTIME_FAIL_CLOSED"
        assert activation["failed_checks"]


if __name__ == "__main__":
    test_protocol_is_exactly_frozen_and_receive_time_gridded()
    test_immutable_baseline_requires_hash_pinned_protocol_report_and_manifest()
    test_external_segments_include_closed_gzip_but_never_open_files()
    test_no_closed_markets_materializes_explicit_ineligible_activation()
    test_runtime_failure_overwrites_any_prior_true_activation()
