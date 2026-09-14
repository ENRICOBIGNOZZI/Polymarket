from __future__ import annotations

import importlib.util
import json
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
START = 1_800_000_005_000
PREPARED = START - 5_000


def manifest(experiment_id: str = "experiment-1") -> dict:
    value = {
        "schema": mod.freezer.SCHEMA, "experiment_id": experiment_id, "code_sha": SHA,
        "window_start_ms": START, "window_end_ms": START + mod.freezer.WINDOW_MS,
        "maximum_post_window_fill_ms": 60_000,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "automatic_promotion": False,
        "required_authority_basis": mod.evaluator.REQUIRED_BASIS,
        "markout_horizons": list(mod.freezer.HORIZONS),
        "evidence_sufficiency": {"minimum_independent_fill_clusters": 20, "minimum_filled_shares": 50.0},
        "freeze_protocol": {
            "manifest_prepared_ms": PREPARED,
            "window_start_lead_ms": mod.freezer.FREEZE_LEAD_MS,
            "window_start_rule": "MANIFEST_ATOMICALLY_PUBLISHED_BEFORE_WINDOW_START",
            "evidence_hash_semantics": mod.freezer.PREFIX_HASH_SEMANTICS,
        },
        "evidence_baselines": {"capture": {
            "started_ms": PREPARED - 10, "completed_ms": PREPARED,
            "hash_semantics": mod.freezer.PREFIX_HASH_SEMANTICS,
        }},
    }
    value["manifest_sha256"] = mod.evaluator.canonical_hash(value, "manifest_sha256")
    return value


class FreezeAfterDeployTests(unittest.TestCase):
    def test_claim_marker_is_exclusive_and_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "marker.json"
            mod.claim_marker(marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=PREPARED)
            self.assertEqual(json.loads(marker.read_text())["state"], "FREEZING")
            with self.assertRaisesRegex(ValueError, "claim_race"):
                mod.claim_marker(marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=PREPARED + 1)

    def test_incomplete_prior_attempt_refuses_duplicate_window(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory); marker, stable = mod.marker_paths(root, RUN_ID)
            mod.claim_marker(marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=PREPARED)
            with self.assertRaisesRegex(ValueError, "incomplete_prior_attempt"):
                mod.existing_result(marker, stable, expected_sha=SHA, deploy_run_id=RUN_ID)

    def test_finalize_persists_identical_manifest_and_frozen_marker(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory); marker, stable = mod.marker_paths(root, RUN_ID)
            mod.claim_marker(marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=PREPARED)
            source = root / "experiment-1" / "manifest.json"; source.parent.mkdir()
            value = manifest(); source.write_text(json.dumps(value))
            result = mod.finalize(marker, stable, source, value,
                expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=PREPARED + 2)
            self.assertEqual(result["state"], "FROZEN")
            self.assertEqual(json.loads(stable.read_text()), value)
            self.assertEqual(json.loads(marker.read_text())["manifest_sha256"], value["manifest_sha256"])

    def test_finalize_rejects_disk_memory_manifest_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory); marker, stable = mod.marker_paths(root, RUN_ID)
            mod.claim_marker(marker, expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=PREPARED)
            source = root / "experiment-1" / "manifest.json"; source.parent.mkdir()
            memory = manifest(); disk = dict(memory); disk["experiment_id"] = "other"
            disk["manifest_sha256"] = mod.evaluator.canonical_hash(disk, "manifest_sha256")
            source.write_text(json.dumps(disk))
            with self.assertRaisesRegex(ValueError, "experiment_id|disk_memory_mismatch"):
                mod.finalize(marker, stable, source, memory,
                    expected_sha=SHA, deploy_run_id=RUN_ID, now_ms=PREPARED + 2)
            self.assertEqual(json.loads(marker.read_text())["state"], "FREEZING")

    def test_existing_frozen_window_is_idempotently_reused(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory); marker, stable = mod.marker_paths(root, RUN_ID); stable.parent.mkdir(parents=True)
            value = manifest(); stable.write_text(json.dumps(value))
            mod.atomic_json(marker, {"schema": mod.SCHEMA, "state": "FROZEN", "code_sha": SHA,
                "deploy_run_id": RUN_ID, "experiment_id": value["experiment_id"],
                "manifest_sha256": value["manifest_sha256"]})
            result = mod.existing_result(marker, stable, expected_sha=SHA, deploy_run_id=RUN_ID)
            self.assertEqual(result["state"], "ALREADY_FROZEN_IDEMPOTENT")

    def test_existing_marker_rejects_wrong_deploy_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory); marker, stable = mod.marker_paths(root, RUN_ID); stable.parent.mkdir(parents=True)
            value = manifest(); stable.write_text(json.dumps(value))
            mod.atomic_json(marker, {"state": "FROZEN", "code_sha": "b" * 40,
                "deploy_run_id": RUN_ID, "experiment_id": value["experiment_id"],
                "manifest_sha256": value["manifest_sha256"]})
            with self.assertRaisesRegex(ValueError, "marker:identity"):
                mod.existing_result(marker, stable, expected_sha=SHA, deploy_run_id=RUN_ID)

    def test_wait_for_evidence_accepts_empty_ledger_but_requires_other_streams(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = mod.required_evidence_paths(root)
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text("x\n")
            paths[0].write_bytes(b"")
            self.assertTrue(mod.evidence_surfaces_ready(root))
            mod.wait_for_evidence(root, timeout_seconds=0, poll_seconds=.01)
            paths[-1].write_text("")
            with self.assertRaisesRegex(ValueError, "evidence:not_ready"):
                mod.wait_for_evidence(root, timeout_seconds=0, poll_seconds=.01)

    def test_freeze_after_deploy_calls_freezer_once_after_preflight(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory); repo = root / "repo"; run = root / "run"; out = root / "out"
            repo.mkdir(); run.mkdir(); out.mkdir()
            for path in mod.required_evidence_paths(run):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text("x\n")
            value = manifest("experiment-x"); source = out / "experiment-x" / "manifest.json"
            source.parent.mkdir(); source.write_text(json.dumps(value))
            with (mock.patch.object(mod, "preflight", return_value={"ok": True}) as preflight,
                  mock.patch.object(mod.freezer, "prepare", return_value=value) as prepare,
                  mock.patch.object(mod.time, "time_ns", return_value=PREPARED * 1_000_000)):
                result = mod.freeze_after_deploy(repo, run, out,
                    expected_sha=SHA, deploy_run_id=RUN_ID, wait_seconds=0, poll_seconds=.01)
            self.assertEqual(result["state"], "FROZEN")
            self.assertEqual(prepare.call_count, 1); self.assertEqual(preflight.call_count, 1)
            self.assertNotIn("now_ms", prepare.call_args.kwargs)

    def test_bad_sha_and_run_id_fail_before_freeze(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "expected_sha"):
                mod.freeze_after_deploy(root, root, root, expected_sha="bad", deploy_run_id=RUN_ID, wait_seconds=0)
            with self.assertRaisesRegex(ValueError, "deploy_run_id"):
                mod.freeze_after_deploy(root, root, root, expected_sha=SHA, deploy_run_id="not-a-number", wait_seconds=0)


if __name__ == "__main__":
    unittest.main()
