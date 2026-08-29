import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "apply_reward_payout_floor.py"


class RewardPayoutFloorTest(unittest.TestCase):
    def write_fixture(self, path: Path) -> None:
        fieldnames = [
            "market_id",
            "estimated_native_daily_value",
            "capital_charge_daily",
            "adverse_budget_daily",
            "conservative_daily_score",
        ]
        rows = [
            {
                "market_id": "sub-floor",
                "estimated_native_daily_value": "0.17",
                "capital_charge_daily": "0.02",
                "adverse_budget_daily": "0.12",
                "conservative_daily_score": "0.03",
            },
            {
                "market_id": "payable",
                "estimated_native_daily_value": "1.20",
                "capital_charge_daily": "0.10",
                "adverse_budget_daily": "0.20",
                "conservative_daily_score": "0.90",
            },
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def read_rows(self, path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_floor_preserves_conditional_economics_but_removes_false_standalone_edge(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rewards.csv"
            self.write_fixture(path)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--csv",
                    str(path),
                    "--minimum-daily-payout-usd",
                    "1.0",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn("conditional_positive=2", result.stdout)
            self.assertIn("standalone_positive=1", result.stdout)

            rows = {row["market_id"]: row for row in self.read_rows(path)}
            sub = rows["sub-floor"]
            self.assertAlmostEqual(float(sub["conditional_conservative_daily_score"]), 0.03)
            self.assertAlmostEqual(float(sub["standalone_payable_native_daily_value"]), 0.0)
            self.assertAlmostEqual(float(sub["payout_shortfall_usd"]), 0.83)
            self.assertAlmostEqual(float(sub["conservative_daily_score"]), -0.14)
            self.assertAlmostEqual(float(sub["minimum_daily_payout_usd"]), 1.0)

            payable = rows["payable"]
            self.assertAlmostEqual(float(payable["standalone_payable_native_daily_value"]), 1.2)
            self.assertAlmostEqual(float(payable["payout_shortfall_usd"]), 0.0)
            self.assertAlmostEqual(float(payable["conservative_daily_score"]), 0.9)

    def test_reprocessing_is_idempotent_and_keeps_payout_aware_ranking(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rewards.csv"
            self.write_fixture(path)
            command = [sys.executable, str(SCRIPT), "--csv", str(path)]
            subprocess.run(command, check=True, capture_output=True, text=True)
            first = path.read_text(encoding="utf-8")
            subprocess.run(command, check=True, capture_output=True, text=True)
            second = path.read_text(encoding="utf-8")
            self.assertEqual(first, second)
            rows = self.read_rows(path)
            self.assertEqual(rows[0]["market_id"], "payable")
            self.assertEqual(rows[1]["market_id"], "sub-floor")

    def test_invalid_input_is_removed_instead_of_leaving_pre_floor_scores(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rewards.csv"
            path.write_text(
                "market_id,estimated_native_daily_value,capital_charge_daily,conservative_daily_score\n"
                "unsafe,0.50,0.01,0.49\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--csv", str(path)],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reward_payout_floor_failed", result.stderr)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
