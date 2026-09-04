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
_DYNAMIC_REVIEW_CLASSIFICATIONS = {
    "MERGE_INTO_CANONICAL",
    "DELETE_ACTIVE_LEGACY",
}
_FORBIDDEN_REVIEW_CAPABILITIES = {
    "submit",
    "submit_orders",
    "cancel_orders",
    "sign",
    "sign_orders",
    "authenticated_execution",
    "real_order_submission",
    "inventory_authority",
    "oms_authority",
    "capital_authority",
    "ledger_writer_authority",
}


def _dynamic_review_ref(surface_id: str) -> bool:
    """Return true only for temporary V7 review branches."""
    return surface_id.startswith(_DYNAMIC_REVIEW_REF_PREFIXES)


def _assert_fail_closed_review_ref(
    testcase: unittest.TestCase, key: str, row: dict,
) -> None:
    testcase.assertEqual(row["object_type"], "branch_or_remote_ref", key)
    testcase.assertIn(row["classification"], _DYNAMIC_REVIEW_CLASSIFICATIONS, key)
    authority = row.get("economic_authority") or {}
    testcase.assertFalse(authority.get("executable"), key)
    capabilities = {
        str(value).strip().lower()
        for value in (authority.get("capabilities") or [])
    }
    testcase.assertTrue(
        capabilities.isdisjoint(_FORBIDDEN_REVIEW_CAPABILITIES),
        f"{key}: forbidden executable capability {sorted(capabilities & _FORBIDDEN_REVIEW_CAPABILITIES)}",
    )
    testcase.assertTrue(str(row.get("migration_status") or "").strip(), key)


class SurfaceClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = json.loads(
            (ROOT / "artifacts/v7_unification/path_classification.json").read_text()
        )

    def test_manifest_covers_audited_paths_refs_schemas_workflows_processes_and_outputs(self) -> None:
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
            _assert_fail_closed_review_ref(self, key, row)
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
            if _dynamic_review_ref(key):
                _assert_fail_closed_review_ref(self, key, row)

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
