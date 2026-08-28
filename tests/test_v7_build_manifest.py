from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_build_manifest", ROOT / "scripts/v7_build_manifest.py")
assert SPEC and SPEC.loader
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)
SHA = "b" * 40


class V7BuildManifestTests(unittest.TestCase):
    def test_manifest_binds_toolchain_dependencies_platform_and_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "polymarket_v7_runtime"
            binary.write_bytes(b"ELF-test-binary")
            with mock.patch.object(build, "_tool_identity", side_effect=lambda name: {"command": name, "version": name + " 1.0"}), \
                 mock.patch.object(build, "_dependency", side_effect=lambda name, pkg: {"name": name, "version": "1.0", "discovery": "pkg-config:" + pkg}), \
                 mock.patch.object(build, "_boost_dependency", return_value={"name": "Boost", "version": "1.85", "discovery": "test"}):
                value = build.build_manifest(
                    binaries=[binary], code_sha=SHA, build_type="Release", compiler="c++",
                    repository_root=root, timestamp="2026-08-28T20:00:00Z",
                    build_flags=["-O3"], extra_dependencies=["Boost=boost"],
                )
            self.assertEqual(value["code_sha"], SHA)
            self.assertEqual(value["build_type"], "Release")
            self.assertEqual(value["sbom"]["format"], "CycloneDX")
            self.assertEqual(value["binaries"][0]["path"], "polymarket_v7_runtime")
            build.validate_manifest(value)

            tampered = json.loads(json.dumps(value))
            tampered["binaries"][0]["sha256"] = "0" * 64
            with self.assertRaises(build.ManifestError):
                build.validate_manifest(tampered)

    def test_empty_binary_set_is_rejected(self) -> None:
        with self.assertRaisesRegex(build.ManifestError, "binaries:empty"):
            build.build_manifest(
                binaries=[], code_sha=SHA, build_type="Release", compiler="c++",
                repository_root=ROOT, timestamp="2026-08-28T20:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
