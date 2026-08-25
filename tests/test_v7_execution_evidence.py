from __future__ import annotations

import csv
import importlib.util
import json
import math
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
                root / "micro_taker" / "execution_events.csv",
                ["timestamp", "position_id", "action", "pnl", "fee", "market_id", "experiment_kind"],
                [{key: value for key, value in row.items() if key != "markout"} for row in rows],
            )
            write_csv(
                root / "micro_taker" / "markouts.csv",
                ["timestamp", "position_id", "markout_pnl", "market_id", "experiment_kind"],
                [
                    {
                        "timestamp": row["timestamp"],
                        "position_id": f"position-{index}",
                        "markout_pnl": row["markout"],
                        "market_id": row["market_id"],
                        "experiment_kind": "ALPHA",
                    }
                    for index, row in enumerate(rows, start=1)
                ],
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

    def test_terminal_model_requires_resolved_calibration_against_market(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = policy_for_test()
            policy["models"]["external"]["min_terminal_observations"] = 1
            report = evidence.build_report(Path(temporary), policy, now=1_700_000_000)
        row = report["models"]["external"]
        self.assertIn("terminal_calibration_unverifiable", row["reason_codes"])
        self.assertIn("terminal_brier_improvement_gate", row["reason_codes"])

    def test_tiny_exploration_rows_are_visible_but_cannot_promote_micro_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_csv(
                root / "micro_taker" / "execution_events.csv",
                ["timestamp", "position_id", "action", "pnl", "fee", "market_id", "experiment_kind"],
                [
                    {"timestamp": 10 * 86400 + 1, "position_id": "p1", "action": "SELL_EXPLORATION", "pnl": "10", "fee": "0.1", "market_id": "m1", "experiment_kind": "EXPLORATION"},
                    {"timestamp": 11 * 86400 + 1, "position_id": "p2", "action": "SELL_EXPLORATION", "pnl": "10", "fee": "0.1", "market_id": "m2", "experiment_kind": "EXPLORATION"},
                ],
            )
            write_csv(
                root / "micro_taker" / "markouts.csv",
                ["timestamp", "experiment_kind", "markout_pnl", "market_id"],
                [
                    {"timestamp": 10 * 86400 + 1, "experiment_kind": "EXPLORATION", "markout_pnl": "2", "market_id": "m1"},
                    {"timestamp": 11 * 86400 + 1, "experiment_kind": "EXPLORATION", "markout_pnl": "2", "market_id": "m2"},
                ],
            )
            policy = policy_for_test()
            policy["models"]["micro_taker"].update({"min_fills": 1, "min_pnl_observations": 1, "min_markout_observations": 1})
            report = evidence.build_report(root, policy, now=1_700_000_000)
        micro = report["models"]["micro_taker"]
        self.assertFalse(micro["paper_eligible"])
        self.assertEqual(micro["fills"], 0)
        self.assertEqual(micro["exploration_rows_excluded"], 4)
        self.assertIn("insufficient_fills", micro["reason_codes"])

    def test_production_admission_and_execution_schemas_are_used_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_csv(
                root / "maker" / "maker_order_log.csv",
                ["timestamp", "action", "market_id", "signal_edge"],
                [
                    {"timestamp": 1, "action": "POST", "market_id": "m1", "signal_edge": 0.01},
                    {"timestamp": 2, "action": "CANCEL", "market_id": "m1", "signal_edge": 0.01},
                    {"timestamp": 3, "action": "POST", "market_id": "m2", "signal_edge": 0.01},
                ],
            )
            # This compatibility feed is input to the external model, not an
            # execution admission ledger, and must lose to engine signals.
            write_csv(
                root / "external_signals.csv",
                ["market_key", "q_yes", "confidence", "source", "timestamp"],
                [{"market_key": "raw", "q_yes": 0.7, "confidence": 0.8, "source": "research", "timestamp": 1}],
            )
            write_csv(
                root / "external" / "signals.csv",
                ["timestamp", "market_id", "net_edge", "desired_notional"],
                [
                    {"timestamp": 1, "market_id": "admitted", "net_edge": 0.01, "desired_notional": 5},
                    {"timestamp": 2, "market_id": "sized-out", "net_edge": -0.01, "desired_notional": 0},
                ],
            )

            report = evidence.build_report(root, policy_for_test(), now=1_700_000_000)

        maker = report["models"]["micro_maker"]
        external = report["models"]["external"]
        self.assertEqual(maker["orders_submitted"], 2)
        self.assertTrue(any(source["path"].endswith("maker/maker_order_log.csv") for source in maker["sources"]))
        self.assertEqual(external["orders_submitted"], 1)
        self.assertEqual(len(external["sources"]), 2)
        self.assertTrue(external["sources"][1]["path"].endswith("external/signals.csv"))

    def test_relative_value_counts_unique_bundles_and_only_observed_adverse_marks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fields = [
                "bundle_id", "closed_ts", "status", "fill_fraction", "net_pnl",
                "fees", "slippage", "adverse_mark_pnl",
            ]
            write_csv(
                root / "bundle_ledger.csv",
                fields,
                [
                    {"bundle_id": "b1", "closed_ts": 10 * 86400, "status": "CLOSED", "fill_fraction": 0.5, "net_pnl": 99, "fees": 9, "slippage": 9, "adverse_mark_pnl": 9},
                    {"bundle_id": "b1", "closed_ts": 10 * 86400 + 1, "status": "CLOSED", "fill_fraction": 1, "net_pnl": 2, "fees": 0.2, "slippage": 0.1, "adverse_mark_pnl": 0.25},
                    {"bundle_id": "b2", "closed_ts": 11 * 86400 + 1, "status": "UNWOUND", "fill_fraction": 0.5, "net_pnl": 1, "fees": 0.1, "slippage": 0.05, "adverse_mark_pnl": 0},
                ],
            )
            write_csv(
                root / "intents.csv",
                ["bundle_id", "market_id", "expected_edge"],
                [
                    {"bundle_id": "b1", "market_id": "m1", "expected_edge": 0.01},
                    {"bundle_id": "b1", "market_id": "m2", "expected_edge": 0.01},
                    {"bundle_id": "b2", "market_id": "m3", "expected_edge": 0.01},
                    {"bundle_id": "b2", "market_id": "m4", "expected_edge": 0.01},
                ],
            )

            report = evidence.build_report(root, policy_for_test(), now=1_700_000_000)

        relative = report["models"]["relative_value"]
        self.assertEqual(relative["orders_submitted"], 2)
        self.assertEqual(relative["fills"], 2)
        self.assertEqual(relative["realized_pnl_observations"], 2)
        self.assertEqual(relative["net_pnl"], 3)
        self.assertEqual(relative["forward_markout_observations"], 1)
        self.assertEqual(relative["mean_forward_markout"], 0.25)
        self.assertAlmostEqual(relative["stressed_net_pnl"], 2.775)

    def test_hard_principal_cost_is_not_execution_friction(self) -> None:
        self.assertTrue(math.isnan(evidence.explicit_cost({"cost": "25"})))
        self.assertEqual(evidence.explicit_cost({"cost": "25", "fee": "0.4"}), 0.4)

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
