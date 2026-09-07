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


if __name__ == "__main__":
    test_protocol_is_exactly_frozen_and_receive_time_gridded()
    test_no_closed_markets_materializes_explicit_ineligible_activation()
    test_runtime_failure_overwrites_any_prior_true_activation()
