from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class V7VerifyContractTests(unittest.TestCase):
    def test_one_command_contains_all_required_verification_domains(self) -> None:
        path = ROOT / "scripts/verify_v7.sh"
        completed = subprocess.run(["bash", "-n", str(path)], check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        text = path.read_text(encoding="utf-8")
        for required in (
            "missing required command", "CMAKE_BUILD_TYPE=Release", "CMAKE_BUILD_TYPE=Debug",
            "ctest --test-dir", "test_no_legacy_runtime.py", "test_v7_single_writer_contract.py",
            "test_v7_*manifest.py", "pytest --quiet tests/test_v7_*.py",
            "pytest --quiet tests/test_monitoring_v7_*.py", "v7_latency_gate.py",
            "v7_build_manifest.py create", "tracked-secret scan passed",
            "GitHub Action pin validation passed",
            "V7_VERIFY_SANITIZERS", "V7_VERIFY_TSAN",
        ):
            self.assertIn(required, text)

    def test_security_and_contribution_surfaces_exist(self) -> None:
        for relative in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md", ".github/CODEOWNERS"):
            self.assertTrue((ROOT / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
