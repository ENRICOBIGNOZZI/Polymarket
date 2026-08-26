import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v7_fast_joint_execution_evidence.py"
spec = importlib.util.spec_from_file_location("v7_fast_joint_execution_evidence", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SHA = "a" * 40


def row(**updates):
    value = {
        "model_sha": SHA,
        "joint_state": "ALL_LEGS_FILLED",
        "target_legs": "2",
        "filled_legs": "2",
        "net_pnl": "1.0",
        "locked_terminal_pnl": "",
        "explicit_cost": "0.2",
        "capital_seconds": "60",
        "completed_basket": "true",
        "partial_unwind": "false",
        "unwind_accounted": "true",
        "point_in_time": "true",
        "authoritative_fees": "true",
        "depth_executable": "true",
    }
    value.update(updates)
    return value


class FastJointExecutionEvidenceTest(unittest.TestCase):
    def test_positive_complete_and_partial_rows_are_aggregated(self):
        report = module.aggregate(
            [
                row(),
                row(
                    joint_state="LEG1_ONLY_UNWOUND",
                    target_legs="2",
                    filled_legs="1",
                    completed_basket="false",
                    partial_unwind="true",
                    unwind_accounted="true",
                    net_pnl="-0.1",
                    explicit_cost="0.05",
                    capital_seconds="15",
                ),
            ],
            expected_sha=SHA,
        )
        self.assertEqual(report["joint_state_observations"], 2)
        self.assertEqual(report["realized_pnl_observations"], 2)
        self.assertEqual(report["completed_baskets"], 1)
        self.assertEqual(report["partial_unwind_observations"], 1)
        self.assertTrue(report["partial_unwind_accounted"])
        self.assertAlmostEqual(report["fill_conditioned_net_pnl"], 0.9)
        self.assertAlmostEqual(report["cost_stress_1_5x_net_pnl"], 0.775)
        self.assertAlmostEqual(report["cost_stress_2x_net_pnl"], 0.65)

    def test_open_completed_basket_is_counted_but_not_called_realized(self):
        report = module.aggregate(
            [row(
                joint_state="ALL_LEGS_FILLED_OPEN",
                net_pnl="",
                locked_terminal_pnl="0.8",
                explicit_cost="0.1",
                capital_seconds="0.12",
            )],
            expected_sha=SHA,
        )
        self.assertEqual(report["completed_baskets"], 1)
        self.assertEqual(report["realized_pnl_observations"], 0)
        self.assertEqual(report["locked_terminal_observations"], 1)
        self.assertAlmostEqual(report["locked_terminal_pnl_sum"], 0.8)
        self.assertAlmostEqual(report["fill_conditioned_net_pnl"], 0.0)
        self.assertTrue(report["point_in_time"])
        self.assertTrue(report["authoritative_fees"])
        self.assertTrue(report["depth_executable"])

    def test_mixed_sha_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "mixed_or_wrong_sha"):
            module.aggregate([row(model_sha="b" * 40)], expected_sha=SHA)

    def test_missing_unwind_accounting_fails_contract(self):
        report = module.aggregate(
            [row(
                joint_state="LEG1_ONLY",
                target_legs="2",
                filled_legs="1",
                completed_basket="false",
                partial_unwind="true",
                unwind_accounted="false",
                net_pnl="-0.2",
            )],
            expected_sha=SHA,
        )
        self.assertFalse(report["partial_unwind_accounted"])

    def test_empty_evidence_is_fail_closed(self):
        report = module.aggregate([], expected_sha=SHA)
        self.assertFalse(report["point_in_time"])
        self.assertFalse(report["authoritative_fees"])
        self.assertFalse(report["depth_executable"])
        self.assertFalse(report["partial_unwind_accounted"])
        self.assertEqual(report["realized_pnl_observations"], 0)


if __name__ == "__main__":
    unittest.main()
