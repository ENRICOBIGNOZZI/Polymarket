from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "ops", ROOT / "scripts", ROOT / "monitoring"):
    sys.path.insert(0, str(directory))

from v7_paper_integrity_daemon import run_once  # noqa: E402


def head() -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()


def prepare(root: Path, sha: str) -> tuple[Path, Path]:
    (root / "control" / "allocations").mkdir(parents=True)
    (root / "external_fair").mkdir(parents=True)
    (root / "control" / "allocations" / "manifest.json").write_text(
        json.dumps({
            "engine_budgets": {"CRYPTO_SETTLEMENT_ENGINE": 4000.0}
        }),
        encoding="utf-8",
    )
    deployed = root / "control" / "deployed_sha"
    deployed.write_text(sha + "\n", encoding="utf-8")
    config = root / "retention.json"
    config.write_text(json.dumps({
        "schema": "polymarket_v7_data_retention_v1",
        "disk": {
            "critical_free_ratio": 0.000001,
            "minimum_free_bytes": 1,
        },
        "binary_tapes": {
            "enabled": True,
            "patterns": ["external_fair/raw/*.bin"],
            "archive_directory": "archive/binary_tapes",
            "minimum_closed_age_seconds": 1,
            "allowed_schema_versions": [1, 2, 3],
            "maximum_raw_payload_bytes": 2097152,
        },
    }), encoding="utf-8")
    return deployed, config


class PaperIntegrityDaemonTests(unittest.TestCase):
    def test_empty_exact_sha_account_and_retention_are_operational(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            sha = head()
            deployed, config = prepare(run_root, sha)
            status = run_once(
                ROOT,
                run_root,
                deployed,
                config,
                previous_retention={},
                run_retention=True,
            )
            self.assertTrue(status["complete"])
            self.assertEqual(status["state"], "OPERATIONAL")
            self.assertTrue(status["paper_account"]["complete"])
            self.assertTrue(status["binary_tape_retention"]["complete"])
            self.assertEqual(
                status["paper_account"]["account"]["cash"], 4000.0
            )
            self.assertTrue(status["disk"]["healthy"])

    def test_repository_deployment_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            deployed, config = prepare(run_root, "d" * 40)
            status = run_once(
                ROOT,
                run_root,
                deployed,
                config,
                previous_retention={},
                run_retention=True,
            )
            self.assertFalse(status["complete"])
            self.assertIn(
                "DEPLOYED_SHA_REPOSITORY_HEAD_MISMATCH", status["blockers"]
            )
            self.assertFalse(status["paper_account"]["complete"])

    def test_stale_or_missing_retention_status_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            sha = head()
            deployed, config = prepare(run_root, sha)
            status = run_once(
                ROOT,
                run_root,
                deployed,
                config,
                previous_retention={},
                run_retention=False,
            )
            self.assertFalse(status["complete"])
            self.assertIn(
                "BINARY_TAPE_RETENTION_INCOMPLETE", status["blockers"]
            )
            self.assertIn("BINARY_TAPE_RETENTION_STALE", status["blockers"])


if __name__ == "__main__":
    unittest.main()
