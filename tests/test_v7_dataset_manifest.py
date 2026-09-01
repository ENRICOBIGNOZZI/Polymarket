from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_dataset_manifest", ROOT / "scripts/v7_dataset_manifest.py")
assert SPEC and SPEC.loader
dataset = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dataset)
SHA = "a" * 40


class V7DatasetManifestTests(unittest.TestCase):
    def test_point_in_time_identity_is_complete_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ticks.csv"
            source.write_text("market,event,price\nm1,e1,0.50\nm2,e2,0.60\n", encoding="utf-8")
            universe = root / "universe.json"
            universe.write_text(json.dumps({"markets": ["m1", "m2"]}), encoding="utf-8")
            value = dataset.build_manifest(
                source_paths=[source], collector_sha=SHA, data_sources=["polymarket_ws"],
                point_in_time_status="POINT_IN_TIME",
                start_timestamp="2026-08-28T00:00:00Z",
                end_timestamp="2026-08-28T01:00:00Z",
                receive_start_timestamp="2026-08-28T00:00:00.001Z",
                receive_end_timestamp="2026-08-28T01:00:00.001Z",
                markets=["m2", "m1"], events=["e2", "e1"], missing_data=[],
                known_gaps=["m2:one_ws_gap"], universe_snapshot=universe, base_path=root,
            )
            self.assertEqual(value["row_count"], 2)
            self.assertEqual(value["point_in_time_status"], "POINT_IN_TIME")
            self.assertEqual(value["markets"], ["m1", "m2"])
            self.assertRegex(value["manifest_sha256"], r"^[0-9a-f]{64}$")
            dataset.validate_manifest(value)
            verified = dataset.verify_manifest_sources(value, base_path=root)
            self.assertEqual(verified["verified_source_files"], ["ticks.csv"])
            self.assertTrue(verified["universe_snapshot_verified"])

            tampered = json.loads(json.dumps(value))
            tampered["row_count"] = 3
            with self.assertRaises(dataset.ManifestError):
                dataset.validate_manifest(tampered)

    def test_point_in_time_requires_universe_and_outputs_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ticks.jsonl"
            source.write_text('{"market":"m1"}\n', encoding="utf-8")
            common = dict(
                source_paths=[source], collector_sha=SHA, data_sources=["test"],
                point_in_time_status="POINT_IN_TIME",
                start_timestamp="2026-08-28T00:00:00Z", end_timestamp="2026-08-28T00:01:00Z",
                receive_start_timestamp="2026-08-28T00:00:00Z",
                receive_end_timestamp="2026-08-28T00:01:00Z", markets=["m1"], events=[],
                missing_data=[], known_gaps=[], base_path=root,
            )
            with self.assertRaisesRegex(dataset.ManifestError, "universe_snapshot"):
                dataset.build_manifest(**common)

            common["point_in_time_status"] = "NOT_POINT_IN_TIME"
            value = dataset.build_manifest(**common)
            output = root / "dataset-manifest.json"
            dataset.immutable_write(output, value)
            dataset.immutable_write(output, value)
            changed = dict(value)
            changed["dataset_id"] = "different-id"
            with self.assertRaisesRegex(dataset.ManifestError, "immutable_path_collision"):
                dataset.immutable_write(output, changed)
            symlink = root / "manifest-link.json"
            symlink.symlink_to(output)
            with self.assertRaisesRegex(dataset.ManifestError, "output:symlink"):
                dataset.immutable_write(symlink, value)

    def test_source_verification_detects_content_drift_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ticks.jsonl"
            source.write_text('{"market":"m1"}\n', encoding="utf-8")
            value = dataset.build_manifest(
                source_paths=[source], collector_sha=SHA, data_sources=["test"],
                point_in_time_status="NOT_POINT_IN_TIME",
                start_timestamp="2026-08-28T00:00:00Z", end_timestamp="2026-08-28T00:01:00Z",
                receive_start_timestamp="2026-08-28T00:00:00Z",
                receive_end_timestamp="2026-08-28T00:01:00Z", markets=["m1"], events=[],
                missing_data=[], known_gaps=[], base_path=root,
            )
            source.write_text('{"market":"m2"}\n', encoding="utf-8")
            with self.assertRaisesRegex(dataset.ManifestError, "sha256_mismatch"):
                dataset.verify_manifest_sources(value, base_path=root)

            source.unlink()
            source.symlink_to(root / "outside.jsonl")
            with self.assertRaisesRegex(dataset.ManifestError, "path_symlink"):
                dataset.verify_manifest_sources(value, base_path=root)

    def test_binary_source_with_explicit_count_is_verified_by_its_immutable_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.tape"
            source.write_bytes(b"opaque-captured-bytes")
            value = dataset.build_manifest(
                source_paths=[source], row_counts={"book.tape": 7}, collector_sha=SHA,
                data_sources=["clob_user_ws"], point_in_time_status="NOT_POINT_IN_TIME",
                start_timestamp="2026-08-28T00:00:00Z", end_timestamp="2026-08-28T00:01:00Z",
                receive_start_timestamp="2026-08-28T00:00:00Z",
                receive_end_timestamp="2026-08-28T00:01:00Z", markets=["m1"], events=[],
                missing_data=[], known_gaps=[], base_path=root,
            )
            verified = dataset.verify_manifest_sources(value, base_path=root)
            self.assertEqual(verified["byte_hash_only_source_files"], ["book.tape"])


if __name__ == "__main__":
    unittest.main()
