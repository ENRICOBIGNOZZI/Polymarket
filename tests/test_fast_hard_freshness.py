from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_fast_hard_freshness.py"


class FastHardFreshnessGuardTest(unittest.TestCase):
    def run_guard(self, rows: list[dict[str, object]], fields: list[str]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            opportunities = root / "fast_arb_opportunities.csv"
            with opportunities.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            candidate = root / "candidate.json"
            candidate.write_text(
                json.dumps(
                    {
                        "promotion_ready": True,
                        "candidate_policy": {"promotion_ready": True},
                        "gate_reasons": {"promotion": {}},
                    }
                ),
                encoding="utf-8",
            )
            output_json = root / "guard.json"
            output_md = root / "guard.md"
            subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--opportunities",
                    str(opportunities),
                    "--candidate",
                    str(candidate),
                    "--output-json",
                    str(output_json),
                    "--output-markdown",
                    str(output_md),
                ],
                check=True,
            )
            return json.loads(candidate.read_text(encoding="utf-8"))

    def test_unverified_cross_leg_freshness_cannot_promote(self) -> None:
        report = self.run_guard(
            [{"kind": "NEGRISK_COMPLETE_SET", "hard_arbitrage": 1, "executable": 1}],
            ["kind", "hard_arbitrage", "executable"],
        )
        self.assertEqual(report["hard_executable_observations_raw"], 1)
        self.assertEqual(report["hard_executable_observations_freshness_qualified"], 0)
        self.assertEqual(report["hard_executable_observations_unverified_freshness"], 1)
        self.assertFalse(report["promotion_ready"])
        self.assertFalse(report["candidate_policy"]["promotion_ready"])
        self.assertFalse(
            report["gate_reasons"]["promotion"]["hard_freshness_no_unverified_or_stale"]
        )

    def test_only_bounded_age_and_skew_are_qualified(self) -> None:
        rows = []
        for index in range(51):
            rows.append(
                {
                    "kind": "NEGRISK_COMPLETE_SET",
                    "hard_arbitrage": 1,
                    "executable": 1,
                    "max_leg_book_age_ms": 1200 if index < 50 else 2500,
                    "leg_book_skew_ms": 800 if index < 50 else 200,
                }
            )
        report = self.run_guard(
            rows,
            [
                "kind",
                "hard_arbitrage",
                "executable",
                "max_leg_book_age_ms",
                "leg_book_skew_ms",
            ],
        )
        self.assertEqual(report["hard_executable_observations_freshness_qualified"], 50)
        self.assertEqual(report["hard_executable_observations_stale_or_skewed"], 1)
        self.assertTrue(
            report["gate_reasons"]["promotion"]["hard_freshness_qualified_at_least_50"]
        )
        self.assertFalse(report["promotion_ready"])

    def test_fully_qualified_sample_does_not_override_other_promotion_gates(self) -> None:
        rows = [
            {
                "kind": "BINARY_COMPLETE_SET",
                "hard_arbitrage": 1,
                "executable": 1,
                "max_leg_book_age_ms": 500,
                "leg_book_skew_ms": 100,
            }
            for _ in range(50)
        ]
        report = self.run_guard(
            rows,
            [
                "kind",
                "hard_arbitrage",
                "executable",
                "max_leg_book_age_ms",
                "leg_book_skew_ms",
            ],
        )
        self.assertEqual(report["hard_executable_observations_raw"], 50)
        self.assertEqual(report["hard_executable_observations_freshness_qualified"], 50)
        self.assertTrue(report["promotion_ready"])
        self.assertTrue(
            report["gate_reasons"]["promotion"]["hard_freshness_no_unverified_or_stale"]
        )


if __name__ == "__main__":
    unittest.main()
