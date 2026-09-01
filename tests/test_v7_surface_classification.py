from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_surface_classification import (  # noqa: E402
    ClassificationError,
    build_manifest,
    validate_manifest,
)


class SurfaceClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = json.loads(
            (ROOT / "artifacts/v7_unification/path_classification.json").read_text()
        )

    def test_manifest_covers_current_paths_refs_schemas_workflows_processes_and_outputs(self) -> None:
        report = validate_manifest(self.value, root=ROOT)
        self.assertTrue(report["passed"])
        types = {row["object_type"] for row in self.value["entries"]}
        self.assertTrue({
            "tracked_path", "branch_or_remote_ref", "tag", "schema",
            "workflow", "process", "runtime_output", "external_action",
        } <= types)

    def test_generator_reproduces_classification_at_same_repository_tree(self) -> None:
        generated = build_manifest(ROOT)
        expected = {
            row["surface_id"]: (row["object_type"], row["classification"])
            for row in self.value["entries"]
        }
        actual = {
            row["surface_id"]: (row["object_type"], row["classification"])
            for row in generated["entries"]
        }
        self.assertEqual(actual, expected)
        self.assertEqual(generated["coverage"], self.value["coverage"])

    def test_research_authority_injection_fails_closed(self) -> None:
        value = copy.deepcopy(self.value)
        row = next(
            item for item in value["entries"]
            if item["classification"] == "KEEP_ZERO_AUTHORITY_RESEARCH"
        )
        row["economic_authority"] = {
            "owner": "SECOND_OMS", "capabilities": ["submit"], "executable": True,
        }
        with self.assertRaisesRegex(ClassificationError, "research_authority"):
            validate_manifest(value)

    def test_every_temporary_surface_has_a_deletion_gate(self) -> None:
        rows = [
            row for row in self.value["entries"]
            if row["classification"] == "KEEP_TEMPORARY_COMPATIBILITY"
        ]
        self.assertTrue(rows)
        self.assertTrue(all(row["deletion_gate"] for row in rows))


if __name__ == "__main__":
    unittest.main()
