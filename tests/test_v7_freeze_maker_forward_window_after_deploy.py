from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "v7_freeze_maker_forward_window_after_deploy",
    SCRIPTS / "v7_freeze_maker_forward_window_after_deploy.py",
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

SHA = "a" * 40
RUN_ID = "123456"
START = 1_800_000_000_000


def manifest(experiment_id: str = "experiment-1") -> dict:
    value = {
        "schema": mod.freezer.SCHEMA,
        "experiment_id": experiment_id,
        "code_sha": SHA,
        "window_start_ms": START,
        "window_end_ms": START + mod.freezer.WINDOW_MS,
        "maximum_post_window_fill_ms": 60_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_promotion": False,
        "required_authority_basis": mod.evaluator.REQUIRED_BASIS,
        "markout_horizons": list(mod.freezer.HORIZONS),
        "evidence_sufficiency": {
            "minimum_independent_fill_clusters": 20,
            "minimum_filled_shares": 50.0,
        },
    }
    value["manifest_sha256"] = mod.evaluator.canonical_hash(
        value, "manifest_sha256"
    )
    return value


class FreezeAfterDeployTests(unittest.TestCase):
    def test_claim_marker_is_exclusive_and_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "marker.json"
            mod.claim_marker(
                marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=START
            )
            first = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(first["state"], "FREEZING")
            self.assertEqual(first["code_sha"], SHA)
            with self.assertRaisesRegex(ValueError, "claim_race"):
                mod.claim_marker(
                    marker, expected_sha=SHA,
                    deploy_run_id=RUN_ID, now_ms=START + 1,
                )

    def test_incomplete_prior_attempt_refuses_duplicate_window(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker, stable = mod.marker_paths(root, RUN_ID)
            mod.claim_marker(
                marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=START
            )
            with self.assertRaisesRegex(ValueError, "incomplete_prior_attempt"):
                mod.existing_result(
                    marker, stable,
                    expected_sha=SHA, deploy_run_id=RUN_ID,
                )

    def test_finalize_persists_stable_manifest_and_frozen_marker(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker, stable = mod.marker_paths(root, RUN_ID)
            mod.claim_marker(
                marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=START
            )
            experiment = root / "experiment-1"
            experiment.mkdir()
            source = experiment / "manifest.json"
            value = manifest()
            source.write_text(json.dumps(value), encoding="utf-8")
            result = mod.finalize(
                marker, stable, source, value,
                expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=START + 2,
            )
            self.assertEqual(result["state"], "FROZEN")
            self.assertEqual(result["experiment_id"], "experiment-1")
            self.assertTrue(stable.is_file())
            stored = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(stored["state"], "FROZEN")
            self.assertEqual(stored["manifest_sha256"], value["manifest_sha256"])

    def test_existing_frozen_window_is_idempotently_reused(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker, stable = mod.marker_paths(root, RUN_ID)
            stable.parent.mkdir(parents=True)
            value = manifest()
            stable.write_text(json.dumps(value), encoding="utf-8")
            mod.atomic_json(marker, {
                "schema": mod.SCHEMA,
                "state": "FROZEN",
                "code_sha": SHA,
                "deploy_run_id": RUN_ID,
                "experiment_id": value["experiment_id"],
                "manifest_sha256": value["manifest_sha256"],
            })
            result = mod.existing_result(
                marker, stable,
                expected_sha=SHA, deploy_run_id=RUN_ID,
            )
            self.assertEqual(result["state"], "ALREADY_FROZEN_IDEMPOTENT")
            self.assertEqual(result["manifest_sha256"], value["manifest_sha256"])

    def test_existing_marker_rejects_wrong_deploy_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker, stable = mod.marker_paths(root, RUN_ID)
            stable.parent.mkdir(parents=True)
            value = manifest()
            stable.write_text(json.dumps(value), encoding="utf-8")
            mod.atomic_json(marker, {
                "state": "FROZEN",
                "code_sha": "b" * 40,
                "deploy_run_id": RUN_ID,
                "experiment_id": value["experiment_id"],
                "manifest_sha256": value["manifest_sha256"],
            })
            with self.assertRaisesRegex(ValueError, "marker:identity"):
                mod.existing_result(
                    marker, stable,
                    expected_sha=SHA, deploy_run_id=RUN_ID,
                )

    def test_wait_for_evidence_requires_all_four_nonempty_regular_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in mod.required_evidence_paths(root):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n", encoding="utf-8")
            mod.wait_for_evidence(root, timeout_seconds=0, poll_seconds=0.01)
            mod.required_evidence_paths(root)[-1].write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence:not_ready"):
                mod.wait_for_evidence(root, timeout_seconds=0, poll_seconds=0.01)

    def test_freeze_after_deploy_calls_freezer_once_after_preflight(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"; run = root / "run"; out = root / "out"
            repo.mkdir(); run.mkdir(); out.mkdir()
            for path in mod.required_evidence_paths(run):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n", encoding="utf-8")
            value = manifest("experiment-x")
            experiment = out / "experiment-x"
            experiment.mkdir()
            source = experiment / "manifest.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            with (
                mock.patch.object(mod, "preflight", return_value={"ok": True}) as preflight,
                mock.patch.object(mod.freezer, "prepare", return_value=value) as prepare,
                mock.patch.object(mod.time, "time_ns", return_value=START * 1_000_000),
            ):
                result = mod.freeze_after_deploy(
                    repo, run, out,
                    expected_sha=SHA, deploy_run_id=RUN_ID,
                    wait_seconds=0, poll_seconds=0.01,
                )
            self.assertEqual(result["state"], "FROZEN")
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual(preflight.call_count, 1)
            marker, stable = mod.marker_paths(out, RUN_ID)
            self.assertTrue(marker.is_file())
            self.assertTrue(stable.is_file())

    def test_bad_sha_and_run_id_fail_before_freeze(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "expected_sha"):
                mod.freeze_after_deploy(
                    root, root, root,
                    expected_sha="bad", deploy_run_id=RUN_ID,
                    wait_seconds=0,
                )
            with self.assertRaisesRegex(ValueError, "deploy_run_id"):
                mod.freeze_after_deploy(
                    root, root, root,
                    expected_sha=SHA, deploy_run_id="not-a-number",
                    wait_seconds=0,
                )


if __name__ == "__main__":
    unittest.main()
