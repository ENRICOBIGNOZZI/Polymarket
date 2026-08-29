from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "run_paper.sh"


class V7PaperEntrypointCutoverTest(unittest.TestCase):
    def _run_with_manifest(self, manifest: dict[str, object]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "champion.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            env = os.environ.copy()
            env["POLYMARKET_CHAMPION_MANIFEST"] = str(path)
            return subprocess.run(
                ["bash", str(ENTRYPOINT)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )

    def test_disabled_champion_fails_closed_before_build(self) -> None:
        result = self._run_with_manifest(
            {
                "enabled": False,
                "version": None,
                "loop": None,
                "config": None,
                "run_root": None,
                "paper_only": True,
                "authenticated_execution": False,
            }
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("no operational V7 PAPER champion is enabled", result.stderr)
        self.assertNotIn("cmake", result.stdout.lower())

    def test_non_v7_champion_is_rejected(self) -> None:
        result = self._run_with_manifest(
            {
                "enabled": True,
                "version": 6,
                "loop": "scripts/legacy.sh",
                "config": "config/legacy.json",
                "run_root": "runs/legacy",
                "paper_only": True,
                "authenticated_execution": False,
            }
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("refusing non-V7 PAPER champion", result.stderr)

    def test_authenticated_execution_is_rejected(self) -> None:
        result = self._run_with_manifest(
            {
                "enabled": True,
                "version": 7,
                "loop": "scripts/paper_v7_loop.sh",
                "config": "config/paper_v7.json",
                "run_root": "runs/paper_v7_live",
                "paper_only": True,
                "authenticated_execution": True,
            }
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("PAPER-only/authenticated-execution boundary", result.stderr)

    def test_missing_v7_paths_fail_closed_without_fallback(self) -> None:
        result = self._run_with_manifest(
            {
                "enabled": True,
                "version": 7,
                "loop": "scripts/definitely_missing_v7_loop.sh",
                "config": "config/definitely_missing_v7.json",
                "run_root": "runs/paper_v7_live",
                "paper_only": True,
                "authenticated_execution": False,
            }
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("V7 champion loop is missing", result.stderr)
        self.assertNotIn("paper.example.json", result.stderr)

    def test_path_traversal_is_rejected_before_build(self) -> None:
        result = self._run_with_manifest(
            {
                "enabled": True,
                "version": 7,
                "loop": "scripts/../scripts/anything.sh",
                "config": "config/v7.json",
                "run_root": "runs/paper_v7_live",
                "paper_only": True,
                "authenticated_execution": False,
            }
        )
        self.assertEqual(result.returncode, 78)
        self.assertIn("may not contain parent traversal", result.stderr)

    def test_entrypoint_has_no_deleted_legacy_config_fallback(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertNotIn("config/paper.example.json", text)
        self.assertIn("config/live_champion.json", text)
        self.assertIn('VERSION" != "7', text)


if __name__ == "__main__":
    unittest.main()
