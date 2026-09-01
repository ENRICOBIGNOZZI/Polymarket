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

    def test_path_traversal_and_symlinked_archive_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "input.json"; source.write_text("{}\n", encoding="utf-8")
            for run_id, name in (("..", "report.json"), ("run", ".."), ("run", "nested/report.json")):
                with self.assertRaisesRegex(store.ArtifactStoreError, "invalid_identity"):
                    store.store(root, source, exact_code_sha="a" * 40, run_id=run_id, name=name)
            external = root / "external"; external.mkdir()
            archive = root / "artifacts"; archive.symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(store.ArtifactStoreError, "artifact_path_symlink"):
                store.store(root, source, exact_code_sha="a" * 40, run_id="run", name="report.json")

    def test_source_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "input.json"; source.write_text("{}\n", encoding="utf-8")
            linked = root / "linked.json"; linked.symlink_to(source)
            with self.assertRaisesRegex(store.ArtifactStoreError, "source_or_root_invalid"):
                store.store(root, linked, exact_code_sha="a" * 40, run_id="run", name="report.json")


if __name__ == "__main__":
    unittest.main()
