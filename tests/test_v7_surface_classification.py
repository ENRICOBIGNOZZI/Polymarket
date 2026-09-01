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
    equivalent_ref_surface_ids,
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
        # The checked-in artifact is a complete audit-time ref snapshot.  A
        # clean CI checkout intentionally has fewer local/remote-tracking refs;
        # every surface CI can see must still match the audited classification.
        for key, value in actual.items():
            audited_key = next(
                (candidate for candidate in equivalent_ref_surface_ids(key)
                 if candidate in expected),
                None,
            )
            self.assertIsNotNone(audited_key, key)
            self.assertEqual(value, expected[audited_key], key)
        for field, count in generated["coverage"].items():
            if field != "ref_count":
                self.assertEqual(count, self.value["coverage"][field])

    def test_every_current_ref_is_classified_while_snapshot_extras_are_portable(self) -> None:
        generated = build_manifest(ROOT)
        current = {
            row["surface_id"] for row in generated["entries"]
            if row["object_type"] in {"branch_or_remote_ref", "tag"}
        }
        audited = {
            row["surface_id"] for row in self.value["entries"]
            if row["object_type"] in {"branch_or_remote_ref", "tag"}
        }
        missing = {
            key for key in current
            if not any(candidate in audited for candidate in equivalent_ref_surface_ids(key))
        }
        self.assertEqual(missing, set())

    def test_branch_ref_namespace_aliases_are_portable(self) -> None:
        local = "ref:refs/heads/codex/example"
        remote = "ref:refs/remotes/origin/codex/example"
        self.assertEqual(equivalent_ref_surface_ids(local), (local, remote))
        self.assertEqual(equivalent_ref_surface_ids(remote), (remote, local))
        head = "ref:refs/remotes/origin/HEAD"
        self.assertEqual(equivalent_ref_surface_ids(head), (head,))

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

    def test_no_temporary_compatibility_surface_remains(self) -> None:
        rows = [
            row for row in self.value["entries"]
            if row["classification"] == "KEEP_TEMPORARY_COMPATIBILITY"
        ]
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
