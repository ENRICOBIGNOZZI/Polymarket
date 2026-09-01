import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_platform_contract_archive as archive  # noqa: E402
import v7_platform_drift_monitor as monitor  # noqa: E402


class PlatformContractArchiveTest(unittest.TestCase):
    def _snapshot(self, registry: dict) -> dict:
        return {"observed_at": "2026-08-31T00:00:00Z", "api": registry["api"],
                "contracts": registry["contracts"], "protocol": registry["protocol"],
                "market_contract": registry["market_contract"],
                "data_api": registry["data_api"],
                "market_constraints": registry["market_constraints"]}

    def _documents(self, root: Path, registry: dict) -> dict[str, Path]:
        result = {}
        for index, url in enumerate(registry["official_sources"]):
            path = root / f"source-{index}.txt"
            path.write_bytes(f"official snapshot {index}".encode("ascii"))
            result[url] = path
        return result

    def test_complete_archived_contract_is_immutable_and_verifiable(self) -> None:
        registry_path = ROOT / "config/v7_platform_contract.json"
        registry = monitor.load(registry_path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps(self._snapshot(registry)), encoding="utf-8")
            stored = archive.archive(root, registry_path, snapshot, exact_code_sha="a" * 40, run_id="platform-1",
                                     source_documents=self._documents(root, registry),
                                     now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            manifest = root / stored["archive"]["location"]
            verified = archive.verify(root, registry_path, manifest, archive_sha256=stored["archive"]["sha256"],
                                      now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            self.assertEqual(verified["status"], "HEALTHY")
            self.assertEqual(verified["source_count"], len(registry["official_sources"]))
            repeated = archive.archive(root, registry_path, snapshot, exact_code_sha="a" * 40, run_id="platform-1",
                                       source_documents=self._documents(root, registry),
                                       now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            self.assertEqual(repeated, stored)

    def test_all_official_documents_and_unchanged_artifact_bytes_are_required(self) -> None:
        registry_path = ROOT / "config/v7_platform_contract.json"
        registry = monitor.load(registry_path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps(self._snapshot(registry)), encoding="utf-8")
            documents = self._documents(root, registry)
            documents.pop(registry["official_sources"][0])
            with self.assertRaisesRegex(archive.PlatformArchiveError, "official_source_coverage"):
                archive.archive(root, registry_path, snapshot, exact_code_sha="a" * 40, run_id="platform-1",
                                source_documents=documents, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            documents = self._documents(root, registry)
            stored = archive.archive(root, registry_path, snapshot, exact_code_sha="a" * 40, run_id="platform-1",
                                     source_documents=documents, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            manifest = root / stored["archive"]["location"]
            value = json.loads(manifest.read_text(encoding="utf-8"))
            source_path = root / value["source_documents"][0]["artifact"]["location"]
            source_path.chmod(0o644)
            source_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(archive.PlatformArchiveError, "artifact_pointer_hash"):
                archive.verify(root, registry_path, manifest, archive_sha256=stored["archive"]["sha256"],
                               now=datetime(2026, 8, 31, tzinfo=timezone.utc))

    def test_manifest_refuses_registry_change_and_symlinked_source(self) -> None:
        registry_path = ROOT / "config/v7_platform_contract.json"
        registry = monitor.load(registry_path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps(self._snapshot(registry)), encoding="utf-8")
            documents = self._documents(root, registry)
            external = root / "external.txt"; external.write_text("x", encoding="utf-8")
            linked = root / "linked.txt"; linked.symlink_to(external)
            documents[registry["official_sources"][0]] = linked
            with self.assertRaisesRegex(archive.PlatformArchiveError, "source_or_root_invalid"):
                archive.archive(root, registry_path, snapshot, exact_code_sha="a" * 40, run_id="platform-1",
                                source_documents=documents, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            documents = self._documents(root, registry)
            stored = archive.archive(root, registry_path, snapshot, exact_code_sha="a" * 40, run_id="platform-1",
                                     source_documents=documents, now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            manifest = root / stored["archive"]["location"]
            changed_registry = root / "changed-registry.json"
            changed_registry.write_text(json.dumps({**registry, "last_verified_at": "2026-08-30T00:00:00Z"}), encoding="utf-8")
            with self.assertRaisesRegex(archive.PlatformArchiveError, "manifest_identity"):
                archive.verify(root, changed_registry, manifest, archive_sha256=stored["archive"]["sha256"],
                               now=datetime(2026, 8, 31, tzinfo=timezone.utc))

    def test_manifest_hash_is_an_independent_tamper_boundary(self) -> None:
        registry_path = ROOT / "config/v7_platform_contract.json"
        registry = monitor.load(registry_path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps(self._snapshot(registry)), encoding="utf-8")
            stored = archive.archive(root, registry_path, snapshot, exact_code_sha="a" * 40, run_id="platform-1",
                                     source_documents=self._documents(root, registry),
                                     now=datetime(2026, 8, 31, tzinfo=timezone.utc))
            manifest = root / stored["archive"]["location"]
            manifest.chmod(0o644)
            manifest.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(archive.PlatformArchiveError, "manifest_hash"):
                archive.verify(root, registry_path, manifest, archive_sha256=stored["archive"]["sha256"],
                               now=datetime(2026, 8, 31, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
