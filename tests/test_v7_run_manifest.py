from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = load("v7_run_manifest")
dataset = load("v7_dataset_manifest")
build = load("v7_build_manifest")
SHA = "c" * 40
DEPLOY_SHA = "d" * 40


class V7RunManifestTests(unittest.TestCase):
    def test_run_identity_binds_every_economic_provenance_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rows.jsonl"
            source.write_text('{"market":"m1"}\n', encoding="utf-8")
            universe = root / "universe.json"
            universe.write_text('{"markets":["m1"]}\n', encoding="utf-8")
            dataset_value = dataset.build_manifest(
                source_paths=[source], collector_sha=SHA, data_sources=["polymarket_ws"],
                point_in_time_status="POINT_IN_TIME", universe_snapshot=universe,
                start_timestamp="2026-08-28T00:00:00Z", end_timestamp="2026-08-28T01:00:00Z",
                receive_start_timestamp="2026-08-28T00:00:00Z",
                receive_end_timestamp="2026-08-28T01:00:00Z", markets=["m1"], events=[],
                missing_data=[], known_gaps=[], base_path=root,
            )
            dataset_path = root / "dataset.json"
            dataset.immutable_write(dataset_path, dataset_value)
            binary = root / "polymarket_v7_runtime"
            binary.write_bytes(b"binary")
            with mock.patch.object(build, "_tool_identity", side_effect=lambda name: {"command": name, "version": "1"}), \
                 mock.patch.object(build, "_dependency", side_effect=lambda name, pkg: {"name": name, "version": "1", "discovery": pkg}), \
                 mock.patch.object(build, "_boost_dependency", return_value={"name": "Boost", "version": "1", "discovery": "test"}):
                build_value = build.build_manifest(
                    binaries=[binary], code_sha=SHA, build_type="Release", compiler="c++",
                    repository_root=root, timestamp="2026-08-28T19:59:00Z",
                )
            build_path = root / "build_manifest.json"
            build.immutable_write(build_path, build_value)
            config = root / "paper.json"
            registry = root / "registry.json"
            model = root / "model.bin"
            mapping = root / "mapping.json"
            for path, payload in (
                (config, "{}\n"), (registry, "{}\n"), (model, "model\n"),
                (mapping, '{"condition":"oracle"}\n'),
            ):
                path.write_text(payload, encoding="utf-8")

            value = run.build_manifest(
                code_sha=SHA, deployment_sha=DEPLOY_SHA, config=config,
                strategy_registry=registry, models=[f"maker={model}"],
                dataset_manifests=[dataset_path], universe_snapshot=universe,
                fee_schedule_version="clob-fees-2026-08-28",
                execution_model_version="paper-queue-v7.1", contract_mapping=str(mapping),
                oracle_mapping=hashlib.sha256(b"oracle-map").hexdigest(),
                build_manifest=build_path, start_time="2026-08-28T20:00:00Z",
                host="paper-node-test", repository_root=root,
            )
            self.assertRegex(value["run_id"], r"^v7-20260828T200000Z-[0-9a-f]{12}$")
            self.assertEqual(value["schema"], "polymarket_v7_run_manifest_v2")
            self.assertEqual(value["deployment_sha"], DEPLOY_SHA)
            for field in (
                "code_sha", "config_sha", "model_sha", "dataset_manifest_sha",
                "universe_snapshot_sha", "strategy_registry_sha", "contract_mapping_sha",
                "oracle_mapping_sha", "binary_sha", "build_manifest_sha",
            ):
                self.assertIn(field, value)
            self.assertTrue(value["paper_status"]["paper_only"])
            self.assertFalse(value["paper_status"]["authenticated_execution"])
            run.validate_manifest(value)

            tampered = json.loads(json.dumps(value))
            tampered["execution_model_version"] = "unidentified"
            with self.assertRaisesRegex(run.ManifestError, "run_id:identity_mismatch"):
                run.validate_manifest(tampered)

    def test_dirty_tracked_tree_cannot_be_named_by_head_sha(self) -> None:
        with mock.patch.object(run, "_git", side_effect=[SHA, " M config/paper_v7.json"]):
            with self.assertRaisesRegex(run.ManifestError, "tracked_worktree_dirty"):
                run.repository_identity(ROOT)


if __name__ == "__main__":
    unittest.main()
