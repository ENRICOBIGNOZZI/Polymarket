#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NORMALIZER_SPEC = importlib.util.spec_from_file_location(
    "normalize_forward_maker_history",
    ROOT / "scripts" / "normalize_forward_maker_history.py",
)
assert NORMALIZER_SPEC and NORMALIZER_SPEC.loader
normalizer = importlib.util.module_from_spec(NORMALIZER_SPEC)
NORMALIZER_SPEC.loader.exec_module(normalizer)

ALPHA_SPEC = importlib.util.spec_from_file_location(
    "alpha_factory_for_history_test",
    ROOT / "scripts" / "alpha_factory.py",
)
assert ALPHA_SPEC and ALPHA_SPEC.loader
alpha_factory = importlib.util.module_from_spec(ALPHA_SPEC)
ALPHA_SPEC.loader.exec_module(alpha_factory)


def legacy_row(ts: int = 1, policy: str = "improve1") -> dict:
    return {
        "schema": "polymarket_forward_maker_session_summary_v1",
        "generated_ts": ts,
        "policy_summaries": [
            {
                "policy": policy,
                "probes": 20,
                "pair_fills": 2,
                "one_sided": 3,
                "pnl_ex_rewards": -1.25,
            }
        ],
    }


class NormalizeForwardMakerHistoryTests(unittest.TestCase):
    def test_legacy_policy_summaries_become_alpha_factory_aggregate(self) -> None:
        normalized = normalizer.normalize_run(legacy_row())
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
        normalized = normalizer.normalize_run(run)
        self.assertIs(normalized, run)
        self.assertEqual(normalized["aggregate_by_policy"]["join"]["probes"], 10)

    def test_empty_history_is_valid(self) -> None:
        records, stats = normalizer.normalize_history("\n")
        self.assertEqual(records, [])
        self.assertEqual(stats["nonempty_rows"], 0)
        self.assertEqual(normalizer.history_integrity_errors(stats), [])

    def test_mixed_supported_and_malformed_history_fails_integrity(self) -> None:
        text = json.dumps(legacy_row()) + "\n{not-json}\n"
        records, stats = normalizer.normalize_history(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["malformed_rows"], 1)
        self.assertTrue(normalizer.history_integrity_errors(stats))

    def test_mixed_supported_and_unsupported_dict_fails_integrity(self) -> None:
        text = json.dumps(legacy_row()) + "\n" + json.dumps({"unknown": True}) + "\n"
        records, stats = normalizer.normalize_history(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["unsupported_rows"], 1)
        self.assertTrue(normalizer.history_integrity_errors(stats))

    def test_mixed_supported_and_non_dict_fails_integrity(self) -> None:
        text = json.dumps(legacy_row()) + "\n[]\n"
        records, stats = normalizer.normalize_history(text)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["non_dict_rows"], 1)
        self.assertTrue(normalizer.history_integrity_errors(stats))

    def test_invalid_summary_inside_supported_session_fails_integrity(self) -> None:
        bad = legacy_row()
        bad["policy_summaries"].append("bad")
        records, stats = normalizer.normalize_history(json.dumps(bad) + "\n")
        self.assertEqual(records, [])
        self.assertEqual(stats["invalid_supported_rows"], 1)
        self.assertTrue(normalizer.history_integrity_errors(stats))

    def test_negative_or_impossible_fill_counts_fail_integrity(self) -> None:
        bad = legacy_row()
        bad["policy_summaries"][0]["pair_fills"] = 21
        records, stats = normalizer.normalize_history(json.dumps(bad) + "\n")
        self.assertEqual(records, [])
        self.assertEqual(stats["invalid_supported_rows"], 1)

    def test_normalized_history_reaches_alpha_factory_and_stops_false_run_shortage(self) -> None:
        rows = []
        for index in range(25):
            row = legacy_row(1_800_000_000 + index)
            row["policy_summaries"][0]["pair_fills"] = 0
            row["policy_summaries"][0]["one_sided"] = 1 if index == 0 else 0
            row["policy_summaries"][0]["pnl_ex_rewards"] = -0.01
            rows.append(json.dumps(row))
        normalized, stats = normalizer.normalize_history("\n".join(rows) + "\n")
        self.assertEqual(stats["supported_rows"], 25)
        self.assertEqual(normalizer.history_integrity_errors(stats), [])

        config = json.loads((ROOT / "config" / "alpha_factory.json").read_text(encoding="utf-8"))
        candidates = alpha_factory.forward_candidates(normalized, config["gates"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate_id"], "forward_maker:improve1")
        self.assertEqual(candidates[0]["observations"], 25)

        experiments = alpha_factory.next_experiments(
            {
                "live_smoke_fresh": True,
                "oos": {"selected_trades": 1},
                "b1": {},
                "b2": {},
                "b3_rewards": {},
                "external": {"fresh_rows": 1},
            },
            candidates,
        )
        self.assertNotIn(
            "accumulate_forward_execution_evidence",
            {item["experiment_id"] for item in experiments},
        )


if __name__ == "__main__":
    unittest.main()
