from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PromotionEvidenceBindingWorkflowTests(unittest.TestCase):
    def test_controller_binds_evidence_to_ancestor_and_three_way_code_identity(self):
        text = (ROOT / ".github/workflows/promotion-controller.yml").read_text(encoding="utf-8")
        self.assertIn("git merge-base --is-ancestor", text)
        self.assertIn("actualHeadRefOid", text)
        self.assertIn("bound_source_sha", text)
        self.assertIn("candidate_blob", text)
        self.assertIn("source_blob", text)
        self.assertIn("bound_blob", text)
        self.assertIn('candidate_blob" != "$source_blob" || "$candidate_blob" != "$bound_blob', text)
        self.assertNotIn("--force-with-lease", text)

    def test_merge_repeats_same_binding_checks(self):
        text = (ROOT / ".github/workflows/integration-merge.yml").read_text(encoding="utf-8")
        self.assertIn("git merge-base --is-ancestor", text)
        self.assertIn("actualHeadRefOid", text)
        self.assertIn("bound_source_sha", text)
        self.assertIn("candidate_blob", text)
        self.assertIn("source_blob", text)
        self.assertIn("bound_blob", text)
        self.assertIn('test "$candidate_blob" = "$source_blob" && test "$candidate_blob" = "$bound_blob"', text)
        self.assertIn("--require-approval-label", text)

    def test_evidence_can_be_committed_after_bound_code_without_self_reference(self):
        controller = (ROOT / ".github/workflows/promotion-controller.yml").read_text(encoding="utf-8")
        merge = (ROOT / ".github/workflows/integration-merge.yml").read_text(encoding="utf-8")
        # The evidence's source_head_sha is now the code commit. The actual
        # research head may be a descendant containing only evidence/metadata;
        # both controller and merge require ancestorhood plus blob-for-blob
        # identity across bound code, current research head and candidate.
        for text in (controller, merge):
            self.assertIn("source_head_sha", text)
            self.assertIn("git fetch --quiet --no-tags origin", text)
            self.assertIn("source_actual_head", text)
            self.assertIn("source_bound_head", text)


if __name__ == "__main__":
    unittest.main()
