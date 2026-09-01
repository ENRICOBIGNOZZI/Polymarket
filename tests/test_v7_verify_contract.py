from __future__ import annotations

import subprocess
import tempfile
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
            "ctest --test-dir", "test_v7_repository_shape.py", "test_v7_single_writer_contract.py",
            "test_v7_*manifest.py", "test_monitoring_v7_*.py", "v7_latency_gate.py",
            "v7_build_manifest.py create", "tracked-secret scan passed",
            "GitHub Action pin validation passed",
            "V7_VERIFY_SANITIZERS", "V7_VERIFY_TSAN",
            "detect_leaks=0",
            "v7_protocol_fuzz.py",
            "git status --porcelain=v1 --untracked-files=all",
            "exact-SHA verification requires a clean worktree",
            'BUILD_MANIFEST="$RELEASE_BUILD/build_manifest.json"',
            'rm -f -- "$BUILD_MANIFEST"',
        ):
            self.assertIn(required, text)

    def test_security_and_contribution_surfaces_exist(self) -> None:
        for relative in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md", ".github/CODEOWNERS"):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_dirty_checkout_stops_before_an_exact_sha_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            target = scripts / "verify_v7.sh"
            target.write_text((ROOT / "scripts/verify_v7.sh").read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "V7 test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "v7-test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "scripts/verify_v7.sh"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
            (root / "dirty-source.txt").write_text("modified\n", encoding="utf-8")
            completed = subprocess.run(["bash", str(target)], cwd=root, check=False,
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("exact-SHA verification requires a clean worktree", completed.stderr)


if __name__ == "__main__":
    unittest.main()
