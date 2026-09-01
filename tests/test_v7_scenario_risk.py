from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_scenario_risk", ROOT / "scripts/v7_scenario_risk.py")
assert SPEC and SPEC.loader
risk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(risk)


def book() -> dict:
    return {
        "schema": risk.SCHEMA, "model_sha": "a" * 40,
        "positions": [
            {"position_id": "yes-a", "condition_id": "a", "event_id": "event-1", "category": "crypto", "oracle_id": "oracle-1", "state": "RESTING", "yes_net_pnl_base_units": 100, "no_net_pnl_base_units": -60},
            {"position_id": "yes-b", "condition_id": "b", "event_id": "event-1", "category": "crypto", "oracle_id": "oracle-1", "state": "DELAYED", "yes_net_pnl_base_units": 50, "no_net_pnl_base_units": -40},
            {"position_id": "no-c", "condition_id": "c", "event_id": "event-2", "category": "sports", "oracle_id": "oracle-2", "state": "UNSETTLED", "yes_net_pnl_base_units": -20, "no_net_pnl_base_units": 30},
        ],
        "constraints": {"mutually_exclusive": [["a", "b"]], "implications": [{"if_true": "b", "then_true": "c"}]},
        "stress_cost_base_units": {"delayed_settlement": 1, "market_frozen": 2, "oracle_outage": 3,
                                     "external_feed_outage": 4, "venue_cancel_only": 5, "unwind_impossible": 6,
                                     "reward_removal": 7, "fee_increase": 8, "network_partition": 9},
    }


class ScenarioRiskTests(unittest.TestCase):
    def test_feasible_constraints_and_combined_operational_stress_define_worst_case(self) -> None:
        result = risk.assess(book())
        self.assertEqual(result["state"], "PAPER_SCENARIO_RISK_ONLY")
        self.assertFalse(result["live_execution_authorized"])
        self.assertEqual(result["feasible_outcome_count"], 5)
        self.assertEqual(result["worst_case"]["stress"], "COMBINED_OPERATIONAL_STRESS")
        self.assertEqual(result["worst_case_loss_base_units"], 165)
        self.assertEqual(result["worst_case_by_event_base_units"]["event-1"], -145)

    def test_invalid_constraint_or_missing_stress_is_rejected(self) -> None:
        value = book()
        value["constraints"]["implications"] = [{"if_true": "a", "then_true": "unknown"}]
        with self.assertRaisesRegex(risk.ScenarioRiskError, "implication"):
            risk.assess(value)
        value = book()
        value["stress_cost_base_units"].pop("network_partition")
        with self.assertRaisesRegex(risk.ScenarioRiskError, "stress:shape"):
            risk.assess(value)


if __name__ == "__main__":
    unittest.main()
