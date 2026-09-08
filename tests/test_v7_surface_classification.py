from __future__ import annotations

import copy
import json
import sys
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_surface_classification import (  # noqa: E402
    ClassificationError,
    _refs,
    build_manifest,
    equivalent_ref_surface_ids,
    is_dynamic_review_surface_id,
    is_exact_cutover_tag_surface_id,
    validate_manifest,
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
    return is_dynamic_review_surface_id(surface_id)


def _assert_fail_closed_review_ref(
    testcase: unittest.TestCase, key: str, row: dict,
) -> None:
    if is_exact_cutover_tag_surface_id(key):
        testcase.assertEqual(row["object_type"], "tag", key)
        testcase.assertEqual(row["classification"], "ARCHIVE_HISTORY_ONLY", key)
    else:
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
            current = (row["object_type"], row["classification"])
            audited_key = next(
                (candidate for candidate in equivalent_ref_surface_ids(key)
                 if candidate in expected),
                None,
            )
            if audited_key is not None:
                if current != expected[audited_key] and _dynamic_review_ref(key):
                    # A review branch legitimately transitions from pending
                    # merge to redundant/deletable once main contains it.
                    _assert_fail_closed_review_ref(self, key, row)
                    self.assertIn(
                        expected[audited_key][1], _DYNAMIC_REVIEW_CLASSIFICATIONS, key
                    )
                else:
                    self.assertEqual(current, expected[audited_key], key)
                continue
            self.assertTrue(_dynamic_review_ref(key), key)
            _assert_fail_closed_review_ref(self, key, row)
        expected_tracked = int(
            self.value["coverage"]["tracked_or_intended_path_count"]
        )
        for field, count in generated["coverage"].items():
            if field == "ref_count":
                continue
            expected_count = (
                expected_tracked
                if field == "tracked_or_intended_path_count"
                else self.value["coverage"][field]
            )
            self.assertEqual(count, expected_count, field)

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

    def test_review_namespaces_are_fail_closed_and_explicit(self) -> None:
        for namespace in ("feature", "research", "chore"):
            for prefix in ("refs/heads", "refs/remotes/origin"):
                self.assertTrue(_dynamic_review_ref(
                    f"ref:{prefix}/{namespace}/v7-example"
                ))
        self.assertFalse(_dynamic_review_ref("ref:refs/heads/feature/unsafe"))
        self.assertFalse(_dynamic_review_ref("ref:refs/heads/research/unsafe"))
        self.assertFalse(_dynamic_review_ref("ref:refs/heads/chore/unsafe"))
        # Enumerate explicit fixtures: a clean CI checkout has no local review
        # branches. Test both namespaces without requiring workstation refs.
        # Ref names containing "cutover" must never self-grant authority.
        review_refs = [
            (f"{prefix}/fix/v7-cutover-tag-surface-audit-20260908", "a" * 40)
            for prefix in ("refs/heads", "refs/remotes/origin")
        ]
        with mock.patch("v7_surface_classification._refs", return_value=review_refs):
            generated = build_manifest(ROOT)
        generated_refs = {row["surface_id"]: row for row in generated["entries"]}
        for ref, _ in review_refs:
            review = generated_refs[f"ref:{ref}"]
            self.assertFalse(review["economic_authority"]["executable"])
            self.assertEqual(review["economic_authority"]["capabilities"], [])
        valid = "ref:refs/tags/v7-paper-cutover-" + "a" * 40
        self.assertTrue(_dynamic_review_ref(valid))
        self.assertTrue(is_exact_cutover_tag_surface_id(valid))
        for invalid in (
            "ref:refs/tags/v7-paper-cutover-" + "a" * 39,
            "ref:refs/tags/v7-paper-cutover-" + "a" * 41,
            "ref:refs/tags/v7-paper-cutover-" + "A" * 40,
            "ref:refs/tags/v7-paper-cutover-" + "a" * 40 + "-extra",
            "ref:refs/tags/v7-paper-cutover-latest",
            "ref:refs/tags/unrelated-" + "a" * 40,
        ):
            self.assertFalse(_dynamic_review_ref(invalid), invalid)
            self.assertFalse(is_exact_cutover_tag_surface_id(invalid), invalid)

    def test_symbolic_remote_head_is_not_an_independent_surface(self) -> None:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "audit@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "V7 Audit"], check=True)
            (repo / "x").write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
            head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", head], check=True)
            subprocess.run(["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], check=True)
            refs = {ref for ref, _ in _refs(repo)}
            self.assertIn("refs/remotes/origin/main", refs)
            self.assertNotIn("refs/remotes/origin/HEAD", refs)

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
