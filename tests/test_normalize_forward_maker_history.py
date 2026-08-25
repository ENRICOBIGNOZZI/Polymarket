#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "normalize_forward_maker_history",
    ROOT / "scripts" / "normalize_forward_maker_history.py",
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class NormalizeForwardMakerHistoryTests(unittest.TestCase):
    def test_legacy_policy_summaries_become_alpha_factory_aggregate(self) -> None:
        run = {
            "generated_ts": 123,
            "policy_summaries": [
                {
                    "policy": "improve1",
                    "probes": 20,
                    "pair_fills": 2,
                    "one_sided": 3,
                    "pnl_ex_rewards": -1.25,
                }
            ],
        }
        normalized = module.normalize_run(run)
        self.assertIsNotNone(normalized)
        metrics = normalized["aggregate_by_policy"]["improve1"]
        self.assertEqual(metrics["probes"], 20)
        self.assertAlmostEqual(metrics["pair_fill_rate"], 0.10)
        self.assertAlmostEqual(metrics["one_sided_only_rate"], 0.15)
        self.assertAlmostEqual(metrics["conservative_pnl_ex_rewards_usd"], -1.25)

    def test_canonical_aggregate_is_preserved_without_double_counting(self) -> None:
        run = {
            "generated_ts": 456,
            "aggregate_by_policy": {
                "join": {
                    "probes": 10,
                    "pair_fill_rate": 0.2,
                    "one_sided_only_rate": 0.1,
                    "conservative_pnl_ex_rewards_usd": 0.5,
                }
            },
            "policy_summaries": [
                {
                    "policy": "join",
                    "probes": 99,
                    "pair_fills": 99,
                    "one_sided": 99,
                    "pnl_ex_rewards": 99,
                }
            ],
        }
        normalized = module.normalize_run(run)
        self.assertIs(normalized, run)
        self.assertEqual(normalized["aggregate_by_policy"]["join"]["probes"], 10)

    def test_unsupported_json_records_are_detectable(self) -> None:
        records, parsed = module.normalize_history('{"generated_ts": 1, "unknown": true}\n')
        self.assertEqual(parsed, 1)
        self.assertEqual(records, [])

    def test_empty_history_is_valid(self) -> None:
        records, parsed = module.normalize_history("\n")
        self.assertEqual(parsed, 0)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
