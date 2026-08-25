from __future__ import annotations

import copy
import unittest
from pathlib import Path

from scripts.hard_safety_policy import (
    compare_paper_config,
    compare_runtime_hard_safety,
    is_runtime_hard_safety_surface,
)


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
            {
                "name": "micro",
                "overrides": {
                    "min_net_edge": 0.00005,
                    "max_gross_fraction": 0.40,
                    "max_drawdown": 0.15,
                },
            },
            {
                "name": "pca",
                "overrides": {
                    "pca_min_history": 24,
                    "max_gross_fraction": 0.45,
                    "max_drawdown": 0.15,
                },
            },
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

    def test_v5_incumbent_concentration_contract_is_still_preserved(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.05
        current["max_event_fraction"] = 0.15
        current["max_gross_fraction"] = 0.75
        current["multi_strategy"]["global_max_gross_fraction"] = 0.75
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v5.json"))
        self.assertIn("allowed<=0.025, got 0.05", joined)
        self.assertIn("allowed<=0.08, got 0.15", joined)
        self.assertIn("allowed<=0.45, got 0.75", joined)

    def test_v6_authorized_5_15_70_envelope_is_not_rejected_as_old_contract_weakening(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.05
        current["max_event_fraction"] = 0.15
        current["max_gross_fraction"] = 0.70
        current["multi_strategy"]["global_max_gross_fraction"] = 0.70
        current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"] = 0.70
        self.assertEqual(compare_paper_config(BASE, current, "config/paper_v6.json"), [])

    def test_v6_cannot_exceed_authorized_paper_caps(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_market_fraction"] = 0.051
        current["max_event_fraction"] = 0.151
        current["max_gross_fraction"] = 0.701
        current["multi_strategy"]["global_max_gross_fraction"] = 0.701
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v6.json"))
        self.assertIn("max_market_fraction allowed<=0.05, got 0.051", joined)
        self.assertIn("max_event_fraction allowed<=0.15, got 0.151", joined)
        self.assertIn("max_gross_fraction allowed<=0.7, got 0.701", joined)
        self.assertIn("global_max_gross_fraction allowed<=0.7, got 0.701", joined)

    def test_inherited_child_limit_cannot_be_weakened_by_removing_override(self) -> None:
        current = copy.deepcopy(BASE)
        del current["multi_strategy"]["strategies"][0]["overrides"]["max_gross_fraction"]
        errors = compare_paper_config(BASE, current, "config/paper_v5.json")
        self.assertIn(
            "protected hard-safety limit weakened: config/paper_v5.json:strategy[micro].max_gross_fraction allowed<=0.4, got 0.45",
            errors,
        )

    def test_drawdown_and_paper_only_separation_cannot_be_weakened(self) -> None:
        current = copy.deepcopy(BASE)
        current["max_drawdown"] = 0.20
        current["multi_strategy"]["global_max_drawdown"] = 0.20
        current["multi_strategy"]["paper_only"] = False
        joined = "\n".join(compare_paper_config(BASE, current, "config/paper_v6.json"))
        self.assertIn("max_drawdown allowed<=0.15, got 0.2", joined)
        self.assertIn("global_max_drawdown allowed<=0.15, got 0.2", joined)
        self.assertIn("paper-only separation weakened", joined)

    def test_stricter_hard_safety_is_allowed(self) -> None:
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

    def test_runtime_hard_safety_surfaces_include_loop_and_materializer(self) -> None:
        self.assertTrue(is_runtime_hard_safety_surface("scripts/paper_v6_loop.sh"))
        self.assertTrue(is_runtime_hard_safety_surface("scripts/v6_materialize_configs.py"))
        self.assertFalse(is_runtime_hard_safety_surface("scripts/v6_hf_pressure_research.py"))

    def test_new_runtime_hard_safety_override_is_rejected(self) -> None:
        base = "child={k:x for k,x in cfg.items()}\n"
        current = base + "child['max_market_fraction']=0.06\n"
        errors = compare_runtime_hard_safety(base, current, "scripts/v6_materialize_configs.py")
        self.assertEqual(len(errors), 1)
        self.assertIn("max_market_fraction", errors[0])

    def test_runtime_paper_aggression_without_hard_safety_write_is_allowed(self) -> None:
        base = "run_maker --min-edge 0.00035 --max-order-usd 25\n"
        current = "run_maker --min-edge 0.00005 --max-order-usd 125 --improve-ticks 1\n"
        self.assertEqual(compare_runtime_hard_safety(base, current, "scripts/paper_v6_loop.sh"), [])

    def test_live_paper_promotion_requires_merged_pr_provenance(self) -> None:
        workflow = Path(".github/workflows/v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn('commits/${validated_sha}/pulls', workflow)
        self.assertIn("no merged pull-request provenance", workflow)
        self.assertIn('test "$validated_sha" = "$main_sha"', workflow)


if __name__ == "__main__":
    unittest.main()
