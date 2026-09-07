from __future__ import annotations

import copy
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
    protocol = runtime.build_protocol(exp, maker_model_sha=SHA, maker_model_published_ms=1_000)
    rule = exp["frozen_rule"]
    assert protocol["canonical_rule_sha256"] == runtime.sha256_json(rule)
    assert protocol["canonical_rule"] == rule
    assert protocol["trigger_protocol"] == {"grid_ms": 25}
    assert protocol["development_semantics"] == {"overlap_warmup_ms": 300, "overlap_tail_ms": 1200}
    assert protocol["overlay"] == {"effective_cancel_latency_ms": 100}
    assert protocol["stress"] == {"queue_ahead_multiplier": 3.0, "effective_cancel_latency_ms": 200}
    assert protocol["labels"] == {"horizons_ms": [250, 500, 1000]}


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




def sha(path: Path) -> str:
    return runtime.sha256_file(path)


def seed_episode(rule_hash: str, freeze: int, market: int, index: int) -> dict:
    quote_ms = freeze + 1_000 + market * 10_000 + index
    return {
        "schema": runtime.EPISODE_SCHEMA, "market_id": f"m{market:03d}",
        "quote_id": f"m{market:03d}-q{index}", "quote_receive_ms": quote_ms,
        "maker_model_published_ms": freeze - 1_000, "maker_model_sha": SHA,
        "rule_sha256": rule_hash, "book_tape_schema": 2, "receive_time_causal": True,
        "causality_violations": [], "trigger_applied": True, "quote_size_shares": 5.0,
        "baseline_fill": True, "overlay_fill": False, "baseline_filled_shares": 5.0,
        "overlay_filled_shares": 0.0,
        "baseline_markout_per_share": {"250": -0.08, "500": -0.10, "1000": -0.07},
        "overlay_markout_per_share": {},
        "stress": {"queue_3x_cancel_200ms": {
            "baseline_fill": True, "overlay_fill": False,
            "baseline_filled_shares": 5.0, "overlay_filled_shares": 0.0,
            "baseline_markout_per_share": {"500": -0.05}, "overlay_markout_per_share": {},
        }},
    }


def build_seed_pack(root: Path) -> tuple[Path, list[Path]]:
    runs = root / "runs"
    source = runs / "legacy_seed"
    source.mkdir(parents=True)
    exp = runtime.experiment(REGISTRY)
    rule_hash = runtime.sha256_json(exp["frozen_rule"])
    freeze = runtime.freeze_ms(exp)
    protocol = runtime.build_protocol(exp, maker_model_sha=SHA, maker_model_published_ms=freeze - 1_000)
    protocol_path = source / "protocol.json"; write(protocol_path, protocol)
    external_copy = source / "external.bin"; external_copy.write_bytes(b"immutable-external")
    external_source = str(runs / "old_live" / "external.bin")
    episode_refs = []
    episode_paths = []
    for market in range(30):
        rows = [seed_episode(rule_hash, freeze, market, index) for index in range(2)]
        episode_path = source / f"episodes.{market}.jsonl"
        episode_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        episode_paths.append(episode_path)
        book_manifest = source / f"book.{market}.manifest.json"; write(book_manifest, {"market": market})
        book_segment = source / f"book.{market}.bin"; book_segment.write_bytes(f"book-{market}".encode())
        summary = {
            "schema": runtime.SUMMARY_SCHEMA, "market_id": f"m{market:03d}",
            "market_evaluable": True, "evidence_valid": True, "promotion_eligible_market": True,
            "promotion_boundary_ms": freeze, "market_started_ms": freeze + 1 + market * 10_000,
            "exclusion_reason_codes": [], "causality_violations": [], "rule_sha256": rule_hash,
            "protocol_sha256": sha(protocol_path), "output_sha256": sha(episode_path),
            "manifest": str(book_manifest),
            "source_provenance": {
                "manifest_sha256": sha(book_manifest),
                "book_segments": [{"path": str(book_segment), "sha256": sha(book_segment)}],
                "external_segments": [{"path": external_source, "sha256": sha(external_copy)}],
                "tape_dump_sha256": "f" * 64,
            },
        }
        summary_path = source / f"episodes.{market}.summary.json"; write(summary_path, summary)
        episode_refs.append({
            "episodes": str(episode_path), "episodes_sha256": sha(episode_path),
            "summary": str(summary_path), "summary_sha256": sha(summary_path),
        })
    report = runtime.forward.evaluate(REGISTRY, episode_paths, experiment_id=runtime.EXPERIMENT_ID)
    assert report["state"] == "PASS"
    report_path = source / "report.json"; write(report_path, report)
    manifest = {
        "schema": runtime.SEED_SCHEMA, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "automatic_promotion": False, "market_count": 30,
        "episode_files": episode_refs,
        "external_segments": [{"copy": str(external_copy), "source": external_source, "sha256": sha(external_copy)}],
        "protocol": {"path": str(protocol_path), "sha256": sha(protocol_path)},
        "report": {"path": str(report_path), "sha256": sha(report_path)},
    }
    manifest_path = runs / "seed_manifest.json"; write(manifest_path, manifest)
    return manifest_path, episode_paths


def test_verified_seed_pack_is_recomputed_before_activation_and_corruption_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest, source_episodes = build_seed_pack(root)
        research_root = root / "runs" / "new_durable"
        paths, status = runtime.import_seed_evidence(
            seed_manifest=manifest, registry=REGISTRY, research_root=research_root,
            repository_root=root,
        )
        assert status["state"] == "VERIFIED_PASS"
        assert status["activation_eligible"] is True
        assert status["market_count"] == 30 and len(paths) == 30
        assert all(path.is_file() for path in paths)
        assert {runtime.sha256_file(path) for path in paths} == {
            runtime.sha256_file(source) for source in source_episodes
        }

        source_episodes[0].chmod(0o644)
        source_episodes[0].write_text(source_episodes[0].read_text() + "{}\n", encoding="utf-8")
        runtime._SEED_VALIDATION_CACHE.clear()
        try:
            runtime.import_seed_evidence(
                seed_manifest=manifest, registry=REGISTRY, research_root=research_root,
                repository_root=root,
            )
        except ValueError as exc:
            assert str(exc).startswith("seed_hash_mismatch:episode:")
        else:
            raise AssertionError("corrupted seed episode accepted")


def test_semantic_report_compare_accepts_only_machine_scale_float_roundoff() -> None:
    expected = {
        "state": "PASS", "count": 297, "ok": True,
        "metric": 0.045985559324039804,
        "interval": [0.034682619849294834, 0.05950677014191439],
        "nested": {"stress": 0.04540448581594608},
    }
    actual = copy.deepcopy(expected)
    actual["metric"] = 0.04598555932403981
    actual["interval"][0] = 0.03468261984929484
    actual["nested"]["stress"] = 0.04540448581594609
    matched, max_delta, paths = runtime.semantic_report_compare(expected, actual)
    assert matched is True and paths == [] and 0.0 < max_delta < 1e-15

    drifted = copy.deepcopy(actual)
    drifted["metric"] += 1e-8
    matched, _, paths = runtime.semantic_report_compare(expected, drifted)
    assert matched is False and "$.metric" in paths

    wrong_count = copy.deepcopy(actual)
    wrong_count["count"] = 298
    matched, _, paths = runtime.semantic_report_compare(expected, wrong_count)
    assert matched is False and "$.count" in paths

    wrong_flag = copy.deepcopy(actual)
    wrong_flag["ok"] = 1
    matched, _, paths = runtime.semantic_report_compare(expected, wrong_flag)
    assert matched is False and "$.ok" in paths


def test_launcher_binds_existing_durable_seed_pack_without_new_authority() -> None:
    launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "config/v7_process_manifest.json").read_text(encoding="utf-8"))
    assert 'EXTERNAL_CANCEL_SEED_MANIFEST="${PM_V7_EXTERNAL_CANCEL_SEED_MANIFEST:-$DURABLE_ROOT/research/btc_m5_external_cancel_evidence_manifest_v1.json}"' in launcher
    assert '--seed-manifest "$EXTERNAL_CANCEL_SEED_MANIFEST"' in launcher
    row = next(item for item in manifest["processes"] if item["id"] == "external_cancel_forward_runtime")
    assert "${EXTERNAL_CANCEL_SEED_MANIFEST}" in row["inputs"]
    assert "--seed-manifest" in row["arguments"]
    assert row["profile"] == "observer" and "authority_overrides" not in row


if __name__ == "__main__":
    test_protocol_is_exactly_frozen_and_receive_time_gridded()
    test_no_closed_markets_materializes_explicit_ineligible_activation()
    test_runtime_failure_overwrites_any_prior_true_activation()
    test_verified_seed_pack_is_recomputed_before_activation_and_corruption_fails_closed()
    test_semantic_report_compare_accepts_only_machine_scale_float_roundoff()
    test_launcher_binds_existing_durable_seed_pack_without_new_authority()
