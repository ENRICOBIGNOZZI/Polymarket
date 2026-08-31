import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_artifact_store as store  # noqa: E402


class ArtifactStoreTests(unittest.TestCase):
    def test_store_is_sha_scoped_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "input.json"; source.write_text("{}\n", encoding="utf-8")
            pointer = store.store(root, source, exact_code_sha="a" * 40, run_id="run-1", name="report.json")
            self.assertEqual(pointer["location"], "artifacts/by_sha/" + "a" * 40 + "/run-1/report.json")
            store.store(root, source, exact_code_sha="a" * 40, run_id="run-1", name="report.json")
            source.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(store.ArtifactStoreError, "immutable_path_collision"):
                store.store(root, source, exact_code_sha="a" * 40, run_id="run-1", name="report.json")


if __name__ == "__main__":
    unittest.main()
