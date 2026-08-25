from __future__ import annotations

import copy
import unittest

from scripts.hard_safety_policy import compare_paper_config


BASE = {
    "market_limit": 500,
    "min_liquidity": 25.0,
    "min_net_edge": 0.00025,
    "pca_min_history": 24,
    "max_market_fraction": 0.025,
    "max_event_fraction": 0.08,
    "max_gross_fraction": 0.45,
    "max_drawdown": 0.15,
    "multi_strategy": {
        "paper_only": True,
        "global_max_drawdown": 0.15,
        "global_max_gross_fraction": 0.45,
        "strategies": [
            {"name":"micro","overrides":{"min_net_edge":0.00005,"max_gross_fraction":0.40,"max_drawdown":0.15}},
            {"name":"pca","overrides":{"pca_min_history":24,"max_gross_fraction":0.45,"max_drawdown":0.15}},
        ],
    },
}


class HardSafetyPolicyTest(unittest.TestCase):
    def test_authorized_paper_alpha_aggression_is_allowed(self) -> None:
        current = copy.deepcopy(BASE)
        current["market_limit"] = 1000
        current["min_liquidity"] = 10.0
        current["min_net_edge"] = 0.0
        current["pca_min_history"] = 8
        current["micro_pressure_extrapolation"] = 2.0
        current["semantic_min_similarity"] = 0.55
        current["multi_strategy"]["strategies"][0]["overrides"]["min_net_edge"] = 0.0
        current["multi_strategy"]["strategies"][1]["overrides"]["pca_min_history"] = 8
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v5.json"), [])

    def test_legacy_concentration_and_gross_weakening_is_rejected(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.05
        current["max_event_fraction"] = 0.15
        current["max_gross_fraction"] = 0.75
        current["multi_strategy"]["global_max_gross_fraction"] = 0.75
        current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"] = 0.75
        errors = compare_paper_config(BASE, current, "config/paper_v5.json")
        joined = "\n".join(errors)
        self.assertIn("max_market_fraction 0.025 -> 0.05", joined)
        self.assertIn("max_event_fraction 0.08 -> 0.15", joined)
        self.assertIn("max_gross_fraction 0.45 -> 0.75", joined)
        self.assertIn("multi_strategy.global_max_gross_fraction 0.45 -> 0.75", joined)
        self.assertIn("strategy[micro].max_gross_fraction 0.4 -> 0.75", joined)

    def test_v6_authorized_capital_envelope_is_allowed(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.05
        current["max_event_fraction"] = 0.15
        current["max_gross_fraction"] = 0.70
        current["multi_strategy"]["global_max_gross_fraction"] = 0.70
        # V6 capital is sleeve-isolated, not legacy strategies[] overrides.
        current["multi_strategy"].pop("strategies")
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v6.json"), [])

    def test_v6_capital_envelope_cannot_exceed_authorized_ceilings(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.051
        current["max_event_fraction"] = 0.151
        current["max_gross_fraction"] = 0.71
        current["multi_strategy"]["global_max_gross_fraction"] = 0.71
        current["multi_strategy"].pop("strategies")
        errors = compare_paper_config(BASE, current, "config/paper_v6.json")
        joined = "\n".join(errors)
        self.assertIn("max_market_fraction 0.051 > 0.05", joined)
        self.assertIn("max_event_fraction 0.151 > 0.15", joined)
        self.assertIn("max_gross_fraction 0.71 > 0.7", joined)
        self.assertIn("multi_strategy.global_max_gross_fraction 0.71 > 0.7", joined)

    def test_inherited_child_limit_cannot_be_weakened_by_removing_override(self) -> None:
        current = copy.deepcopy(BASE)
        del current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"]
        errors = compare_paper_config(BASE, current, "config/paper_v5.json")
        self.assertIn("protected hard-safety limit weakened: config/paper_v5.json:strategy[micro].max_gross_fraction 0.4 -> 0.45", errors)

    def test_drawdown_and_paper_only_separation_cannot_be_weakened(self) -> None:
        for path in ("config/paper_v5.json", "config/paper_v6.json"):
            current = copy.deepcopy(BASE)
            current["max_drawdown"] = 0.20
            current["multi_strategy"]["global_max_drawdown"] = 0.20
            current["multi_strategy"]["paper_only"] = False
            if path.endswith("v6.json"):
                current["multi_strategy"].pop("strategies")
            errors = compare_paper_config(BASE, current, path)
            joined = "\n".join(errors)
            self.assertIn("max_drawdown 0.15 -> 0.2", joined)
            self.assertIn("multi_strategy.global_max_drawdown 0.15 -> 0.2", joined)
            self.assertIn("paper-only separation weakened", joined)

    def test_stricter_legacy_hard_safety_is_allowed(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.02
        current["max_event_fraction"] = 0.06
        current["max_gross_fraction"] = 0.40
        current["max_drawdown"] = 0.10
        current["multi_strategy"]["global_max_gross_fraction"] = 0.40
        current["multi_strategy"]["global_max_drawdown"] = 0.10
        current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"] = 0.35
        current["multi_strategy"]["strategies"][0]["overrides"]["max_drawdown"] = 0.10
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v5.json"), [])


if __name__ == "__main__":
    unittest.main()
