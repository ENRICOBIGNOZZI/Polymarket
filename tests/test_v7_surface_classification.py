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
    _classification_for_name,
    build_manifest,
    equivalent_ref_surface_ids,
    validate_manifest,
)


_DYNAMIC_REVIEW_REF_PREFIXES = (
    "ref:refs/heads/codex/v7-",
    "ref:refs/remotes/origin/codex/v7-",
    "ref:refs/heads/feature/v7-",
    "ref:refs/remotes/origin/feature/v7-",
    "ref:refs/heads/fix/v7-",
    "ref:refs/remotes/origin/fix/v7-",
    "ref:refs/heads/research/v7-",
    "ref:refs/remotes/origin/research/v7-",
    "ref:refs/heads/chore/v7-",
    "ref:refs/remotes/origin/chore/v7-",
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
_ADDENDUM_SCHEMA = "polymarket_v7_surface_classification_addendum_v1"
_ADDENDUM_PATH = ROOT / "config/v7_surface_classification_addendum.json"


def _dynamic_review_ref(surface_id: str) -> bool:
    """Return true only for temporary fail-closed V7 review branches."""
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


def _load_addendum() -> dict[str, tuple[str, str]]:
    value = json.loads(_ADDENDUM_PATH.read_text(encoding="utf-8"))
    if (
        value.get("schema") != _ADDENDUM_SCHEMA
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or not isinstance(value.get("base_snapshot_sha"), str)
        or len(value["base_snapshot_sha"]) != 40
    ):
        raise AssertionError("invalid surface-classification addendum safety contract")
    rows = value.get("entries")
    if not isinstance(rows, list) or not rows:
        raise AssertionError("surface-classification addendum requires entries")
    output: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "surface_id", "object_type", "classification",
        }:
            raise AssertionError("surface-classification addendum entry shape")
        identity = row["surface_id"]
        object_type = row["object_type"]
        classification = row["classification"]
        if (
            not isinstance(identity, str)
            or identity in output
            or not identity.startswith("path:")
            or object_type != "tracked_path"
        ):
            raise AssertionError(f"invalid addendum surface: {identity}")
        path = identity.removeprefix("path:")
        if not (ROOT / path).is_file():
            raise AssertionError(f"addendum path missing from repository: {path}")
        if _classification_for_name(path) != classification:
            raise AssertionError(f"addendum classification drift: {identity}")
        output[identity] = (object_type, classification)
    return output


class SurfaceClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.value = json.loads(
            (ROOT / "artifacts/v7_unification/path_classification.json").read_text()
        )
        cls.addendum = _load_addendum()

    def test_manifest_covers_audited_paths_refs_schemas_workflows_processes_and_outputs(self) -> None:
        report = validate_manifest(self.value)
        self.assertTrue(report["passed"])
        types = {row["object_type"] for row in self.value["entries"]}
        self.assertTrue({
            "tracked_path", "branch_or_remote_ref", "tag", "schema",
            "workflow", "process", "runtime_output", "external_action",
        } <= types)

    def test_addendum_contains_only_new_explicit_tracked_surfaces(self) -> None:
        audited = {row["surface_id"] for row in self.value["entries"]}
        self.assertTrue(set(self.addendum).isdisjoint(audited))
        self.assertIn("path:config/v7_surface_classification_addendum.json", self.addendum)

    def test_generator_reproduces_classification_at_same_repository_tree(self) -> None:
        generated = build_manifest(ROOT)
        expected = {
            row["surface_id"]: (row["object_type"], row["classification"])
            for row in self.value["entries"]
        }
        expected.update(self.addendum)
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
                self.assertEqual(current, expected[audited_key], key)
                continue
            self.assertTrue(_dynamic_review_ref(key), key)
            _assert_fail_closed_review_ref(self, key, row)
        expected_tracked = (
            int(self.value["coverage"]["tracked_or_intended_path_count"])
            + len(self.addendum)
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
