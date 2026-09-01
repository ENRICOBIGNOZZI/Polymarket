from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_exact_sha_ci_gate", ROOT / "scripts" / "v7_exact_sha_ci_gate.py")
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class ExactShaCiGateTest(unittest.TestCase):
    def test_latest_successful_attempt_for_every_required_v7_check_is_green(self) -> None:
        value = GATE.receipt("ENRICOBIGNOZZI/Polymarket", "a" * 40, [
            {"id": 1, "name": "ci-v7-Debug", "status": "completed", "conclusion": "failure",
             "completed_at": "2026-08-31T09:00:00Z"},
            {"id": 2, "name": "ci-v7-Debug", "status": "completed", "conclusion": "success",
             "completed_at": "2026-08-31T10:00:00Z"},
            {"id": 3, "name": "ci-v7-Release", "status": "completed", "conclusion": "success",
             "completed_at": "2026-08-31T10:00:00Z"},
        ], 1)
        self.assertTrue(value["exact_sha_ci_green"])
        self.assertEqual(value["checks"]["ci-v7-Debug"]["id"], 2)

    def test_missing_or_failed_required_check_is_not_green(self) -> None:
        value = GATE.receipt("ENRICOBIGNOZZI/Polymarket", "b" * 40, [
            {"id": 1, "name": "ci-v7-Release", "status": "completed", "conclusion": "success",
             "completed_at": "2026-08-31T10:00:00Z"},
        ], 1)
        self.assertFalse(value["exact_sha_ci_green"])
        self.assertIsNone(value["checks"]["ci-v7-Debug"]["id"])

    def test_official_checks_html_parser_requires_both_successful_jobs(self) -> None:
        parser = GATE._ChecksPageParser(GATE.DEFAULT_REQUIRED)
        parser.feed("""
        <div class="d-flex checks-list-item position-relative">
          <div><svg aria-label="This job succeeded"></svg>
          <a href="/owner/repo/actions/runs/10/job/101"><span>ci-v7-Release</span></a></div>
        </div>
        <div class="d-flex checks-list-item position-relative">
          <div><svg aria-label="This job succeeded"></svg>
          <a href="/owner/repo/actions/runs/10/job/102"><span>ci-v7-Debug</span></a></div>
        </div>
        """)
        value = GATE.receipt("owner/repo", "c" * 40, parser.rows, 1)
        self.assertTrue(value["exact_sha_ci_green"])
        self.assertEqual(value["checks"]["ci-v7-Release"]["id"], 101)
        self.assertEqual(
            value["checks"]["ci-v7-Debug"]["details_url"],
            "https://github.com/owner/repo/actions/runs/10/job/102",
        )


if __name__ == "__main__":
    unittest.main()
