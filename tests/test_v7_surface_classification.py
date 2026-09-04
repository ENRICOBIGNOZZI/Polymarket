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


_DYNAMIC_REVIEW_REF_PREFIXES = (
    "ref:refs/heads/codex/v7-",
    "ref:refs/remotes/origin/codex/v7-",
    "ref:refs/heads/fix/v7-",
    "ref:refs/remotes/origin/fix/v7-",
)


def _dynamic_review_ref(surface_id: str) -> bool:
    """Return true only for temporary V7 review branches.

    Tracked paths, tags, canonical refs and legacy refs remain bound to the
    checked-in audit snapshot. Review branches are necessarily created after
    that snapshot; the generator still classifies each of them fail-closed as
    work that must be merged into the canonical V7 line before deployment.
    """
    return surface_id.startswith(_DYNAMIC_REVIEW_REF_PREFIXES)


class SurfaceClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = json.loads(
            (ROOT / "artifacts/v7_unification/path_classification.json").read_text()
        )

    def test_manifest_covers_audited_paths_refs_schemas_workflows_processes_and_outputs(self) -> None:
        # Validate the immutable audit snapshot intrinsically. Current
        # short-lived review refs are checked separately below because their
        # creation necessarily postdates the snapshot.
        report = validate_manifest(self.value)
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
        actual_rows = {
            row["surface_id"]: row for row in generated["entries"]
        }
        # The checked-in artifact is the complete audit-time surface snapshot.
        # A clean CI checkout can contain additional PR refs. Those refs are
        # accepted only under the narrow V7 review namespaces and only when the
        # generator classifies them as noncanonical work with zero authority.
        for key, row in actual_rows.items():
            value = (row["object_type"], row["classification"])
            audited_key = next(
                (candidate for candidate in equivalent_ref_surface_ids(key)
                 if candidate in expected),
                None,
            )
            if audited_key is not None:
                self.assertEqual(value, expected[audited_key], key)
                continue
            self.assertTrue(_dynamic_review_ref(key), key)
            self.assertEqual(
                value,
                ("branch_or_remote_ref", "MERGE_INTO_CANONICAL"),
                key,
            )
            authority = row.get("economic_authority") or {}
            self.assertFalse(authority.get("executable"), key)
            self.assertEqual(authority.get("capabilities"), [], key)
        for field, count in generated["coverage"].items():
            if field != "ref_count":
                self.assertEqual(count, self.value["coverage"][field])

    def test_every_current_ref_is_audited_or_fail_closed_dynamic_review_work(self) -> None:
        generated = build_manifest(ROOT)
        current_rows = {
            row["surface_id"]: row for row in generated["entries"]
            if row["object_type"] in {"branch_or_remote_ref", "tag"}
        }
        audited = {
            row["surface_id"] for row in self.value["entries"]
            if row["object_type"] in {"branch_or_remote_ref", "tag"}
        }
        missing = {
            key for key in current_rows
            if not any(candidate in audited for candidate in equivalent_ref_surface_ids(key))
            and not _dynamic_review_ref(key)
        }
        self.assertEqual(missing, set())
        for key, row in current_rows.items():
            if not _dynamic_review_ref(key):
                continue
            self.assertEqual(row["object_type"], "branch_or_remote_ref", key)
            self.assertEqual(row["classification"], "MERGE_INTO_CANONICAL", key)
            self.assertEqual(row["migration_status"], "PENDING_REVIEW", key)

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
