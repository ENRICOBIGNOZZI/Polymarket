from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "lf_v7_canonical_source_anchor_audit.py"
spec = importlib.util.spec_from_file_location("lf_v7_canonical_source_anchor_audit", MODULE_PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def touch(root: Path, rel: str, content: str = "\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class CanonicalSourceAnchorAuditTests(unittest.TestCase):
    def test_complete_fixture_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in (*mod.LF_REQUIRED, *mod.PCA_REQUIRED, *mod.RANKING_REQUIRED):
                touch(root, path)
            touch(root, mod.RANKING_WORKFLOW, mod.DEFERRED_MARKER)

            report = mod.audit_repo(root)
            self.assertTrue(report["canonical_model_source_ready"])
            self.assertEqual(report["decision"], "CANONICAL_V7_MODEL_SOURCE_READY")
            self.assertEqual(report["research_state"], "READY_FOR_EXACT_SHA_EVIDENCE")

    def test_missing_families_are_reported_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            touch(root, mod.LF_REQUIRED[0])
            touch(root, mod.PCA_REQUIRED[0])
            touch(root, mod.RANKING_REQUIRED[0])

            report = mod.audit_repo(root)
            self.assertFalse(report["canonical_model_source_ready"])
            self.assertEqual(report["decision"], "CANONICAL_V7_MODEL_SOURCE_MISSING")
            self.assertGreater(len(report["families"]["local_factor"]["missing"]), 0)
            self.assertGreater(len(report["families"]["pca"]["missing"]), 0)
            self.assertGreater(len(report["families"]["cross_sectional_ranking"]["missing"]), 0)

    def test_ranking_deferred_contract_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            touch(
                root,
                mod.RANKING_WORKFLOW,
                "echo 'V7 ranking lane is registered but deferred until V7 ranking implementation is present on this revision'\n",
            )
            report = mod.audit_repo(root)
            self.assertTrue(report["ranking_workflow"]["present"])
            self.assertTrue(report["ranking_workflow"]["deferred_until_implementation_lands"])

    def test_workflow_without_marker_does_not_claim_deferred_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            touch(root, mod.RANKING_WORKFLOW, "name: ranking\n")
            report = mod.audit_repo(root)
            self.assertTrue(report["ranking_workflow"]["present"])
            self.assertFalse(report["ranking_workflow"]["deferred_until_implementation_lands"])


if __name__ == "__main__":
    unittest.main()
