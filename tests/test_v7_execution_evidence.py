from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v7_execution_evidence.py"
spec = importlib.util.spec_from_file_location("v7_execution_evidence_test", SCRIPT)
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evidence
spec.loader.exec_module(evidence)


def policy_for_test() -> dict:
    policy = evidence.default_policy()
    policy["bootstrap_samples"] = 200
    for contract in policy["models"].values():
        contract.update(
            {
                "min_fills": 4,
                "min_pnl_observations": 4,
                "min_markout_observations": 0,
                "min_fill_rate": 0.0,
                "min_active_folds": 2,
                "min_positive_fold_fraction": 0.5,
                "max_bootstrap_pvalue": 0.10,
            }
        )
    return policy


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class ExecutionEvidenceTest(unittest.TestCase):
    def test_empty_runtime_fails_closed_without_allocating_capital(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = evidence.build_report(Path(temporary), policy_for_test(), now=1_700_000_000)
        self.assertEqual(report["schema"], evidence.SCHEMA)
        self.assertFalse(report["allow_capital_reallocation"])
        self.assertFalse(report["summary"]["capital_allocation_mutated"])
        maker = report["models"]["micro_maker"]
        self.assertEqual(maker["target"], "short_horizon_markout")
        self.assertEqual(maker["state"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("insufficient_fills", maker["reason_codes"])
        self.assertIn("cost_stress_unverifiable", maker["reason_codes"])

    def test_positive_two_day_paper_ledger_is_eligible_only_for_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = []
            for index, day in enumerate((10, 10, 11, 11), start=1):
                rows.append(
                    {
                        "timestamp": day * 86400 + index,
                        "action": "SELL",
                        "pnl": "2.0",
                        "fee": "0.1",
                        "markout": "0.01",
                        "market_id": f"market-{index}",
                    }
                )
            write_csv(
                root / "micro_taker" / "fills.csv",
                ["timestamp", "action", "pnl", "fee", "markout", "market_id"],
                rows,
            )
            policy = policy_for_test()
            policy["models"]["micro_taker"]["min_markout_observations"] = 4
            report = evidence.build_report(root, policy, now=1_700_000_000)
        micro = report["models"]["micro_taker"]
        self.assertTrue(micro["paper_eligible"])
        self.assertEqual(micro["target"], "short_horizon_markout")
        self.assertGreater(micro["net_pnl"], 0)
        self.assertGreater(micro["stressed_net_pnl"], 0)
        self.assertEqual(micro["active_folds"], 2)
        self.assertFalse(micro["allocation_mutated"])

    def test_terminal_target_cannot_be_mixed_or_pass_with_invalid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = policy_for_test()
            policy["models"]["external"]["target"] = "not_a_target"
            policy["models"]["external"]["allow_terminal_mixture"] = True
            report = evidence.build_report(Path(temporary), policy, now=1_700_000_000)
        row = report["models"]["external"]
        self.assertFalse(row["paper_eligible"])
        self.assertIn("invalid_target_contract", row["reason_codes"])
        self.assertIn("terminal_mixture_forbidden", row["reason_codes"])

    def test_cli_writes_atomic_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(policy_for_test()), encoding="utf-8")
            output = root / "report.json"
            markdown = root / "report.md"
            self.assertEqual(
                evidence.main(
                    [
                        "--run-root", str(root), "--policy", str(policy_path),
                        "--output", str(output), "--markdown", str(markdown), "--now", "1700000000",
                    ]
                ),
                0,
            )
            self.assertTrue(output.exists())
            self.assertTrue(markdown.exists())
            self.assertIn("Execution evidence", markdown.read_text(encoding="utf-8"))
            self.assertFalse(json.loads(output.read_text())["allow_capital_reallocation"])


if __name__ == "__main__":
    unittest.main()
