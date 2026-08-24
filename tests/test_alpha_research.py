#!/usr/bin/env python3
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from alpha_research import (  # noqa: E402
    ConfigError,
    load_config,
    promotion_gate,
    screen_candidate,
    select_challengers,
    summarize_scan,
)


class AlphaResearchTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        cfg = {
            "schema": "polymarket_alpha_research_v1",
            "cadence_seconds": 3600,
            "max_challengers_per_cycle": 2,
            "champions": {
                "B1": {"id": "b1_champion", "params": {}},
                "B2": {"id": "b2_champion", "params": {}},
            },
            "challengers": [
                {"id": "a", "family": "B1", "params": {"min_z": 1.25}},
                {"id": "b", "family": "B2", "params": {"max_hedges": 3}},
                {"id": "c", "family": "B1", "params": {"max_half_life_hours": 72}},
            ],
        }
        path = root / "cfg.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_rotation_is_deterministic_and_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config(self._config(Path(td)))
            idx0, first = select_challengers(cfg, 0)
            idx1, second = select_challengers(cfg, 3600)
            self.assertEqual(idx0, 0)
            self.assertEqual([x.candidate_id for x in first], ["a", "b"])
            self.assertEqual(idx1, 1)
            self.assertEqual([x.candidate_id for x in second], ["c", "a"])

    def test_config_rejects_unbounded_or_unknown_parameters(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._config(Path(td))
            obj = json.loads(path.read_text())
            obj["challengers"][0]["params"] = {"min_z": 0.1, "invented": 1}
            path.write_text(json.dumps(obj))
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_scan_summary_and_incremental_screen(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            fields = ["maker_entry_net_edge", "taker_net_edge", "raw_expected_edge", "executable_notional", "stability"]
            champ = td / "champ.csv"
            cand = td / "cand.csv"
            with champ.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
                w.writerow(dict(maker_entry_net_edge=.001, taker_net_edge=-.001, raw_expected_edge=.01, executable_notional=100, stability=.8))
            with cand.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
                w.writerow(dict(maker_entry_net_edge=.003, taker_net_edge=.001, raw_expected_edge=.01, executable_notional=100, stability=.9))
                w.writerow(dict(maker_entry_net_edge=.002, taker_net_edge=-.001, raw_expected_edge=.01, executable_notional=100, stability=.8))
            cm = summarize_scan(champ, "B1", 250)
            xm = summarize_scan(cand, "B1", 250)
            ok, failures, evidence = screen_candidate(xm, cm, {
                "min_rows": 1,
                "min_maker_positive": 1,
                "min_best_maker_edge": .0005,
                "min_positive_executable_notional": 25,
                "max_top1_notional_share": .9,
                "min_score_improvement_ratio": 1.05,
                "min_score_improvement": 0,
            })
            self.assertTrue(ok, failures)
            self.assertGreater(evidence["absolute_improvement"], 0)
            self.assertEqual(xm["maker_positive"], 2)

    def test_no_promotion_without_oos(self):
        ok, failures, evidence = promotion_gate(None, {"oos": {}}, {}, 3)
        self.assertFalse(ok)
        self.assertIn("missing_challenger_oos", failures)
        self.assertEqual(evidence, {})

    def test_promotion_requires_incremental_stressed_oos_and_multiplicity(self):
        champion = {
            "oos": {"trades": 40, "mean_return": .002, "max_drawdown": .03},
            "oos_cost_stress": {"mean_return": .001},
            "eligible_for_tiny_pilot": True,
            "bootstrap_one_sided_pvalue": .02,
            "positive_active_folds": 3,
        }
        challenger = {
            "oos": {"trades": 60, "mean_return": .004, "max_drawdown": .025},
            "oos_cost_stress": {"mean_return": .002},
            "eligible_for_tiny_pilot": True,
            "bootstrap_one_sided_pvalue": .02,
            "positive_active_folds": 3,
        }
        gates = {
            "min_oos_trades": 30,
            "min_incremental_mean_return": 0,
            "min_incremental_stressed_mean_return": 0,
            "max_drawdown_increase": 0,
            "max_familywise_pvalue": .10,
            "min_positive_active_folds": 2,
        }
        ok, failures, evidence = promotion_gate(challenger, champion, gates, 3)
        self.assertTrue(ok, failures)
        self.assertAlmostEqual(evidence["multiplicity_adjusted_pvalue"], .06)

        challenger["bootstrap_one_sided_pvalue"] = .04
        ok, failures, _ = promotion_gate(challenger, champion, gates, 3)
        self.assertFalse(ok)
        self.assertIn("multiple_testing_gate", failures)

    def test_champion_env_and_draft_promotion_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cfg_path = self._config(td)
            env_script = ROOT / "scripts" / "alpha_config_env.py"
            completed = subprocess.run(
                [sys.executable, str(env_script), "--config", str(cfg_path)],
                check=True, capture_output=True, text=True,
            )
            env = dict(line.split("=", 1) for line in completed.stdout.splitlines())
            self.assertEqual(env["B1_MIN_Z"], "1.5")
            self.assertEqual(env["B2_MAX_HEDGES"], "4")
            self.assertEqual(env["B1_EXECUTION_MIN_EDGE"], "0.001")

            report = {
                "schema": "polymarket_alpha_research_v1",
                "source_sha": "abc123",
                "cycle_index": 9,
                "production_modified": False,
                "candidates": [{
                    "id": "a", "family": "B1", "stage": "promotion_ready",
                    "hypothesis": "test",
                    "params": {
                        "markets": 600, "history_universe": 160, "lookback_hours": 336,
                        "fidelity_minutes": 30, "min_z": 1.25,
                        "max_half_life_hours": 168.0, "min_t_reversion": 1.75, "top": 80
                    },
                    "execution_min_edge": 0.001,
                    "screen": {"absolute_improvement": .1},
                    "promotion": {
                        "incremental_mean_return": .002,
                        "incremental_stressed_mean_return": .001,
                    },
                }],
            }
            report_path = td / "report.json"
            report_path.write_text(json.dumps(report))
            promotion = ROOT / "scripts" / "promote_alpha_candidate.py"
            subprocess.run([
                sys.executable, str(promotion), "--report", str(report_path),
                "--config", str(cfg_path), "--expected-source-sha", "abc123",
                "--now", "1800000000"
            ], check=True, capture_output=True, text=True)
            promoted = json.loads(cfg_path.read_text())
            self.assertEqual(promoted["champions"]["B1"]["params"]["min_z"], 1.25)
            self.assertNotIn("a", {x["id"] for x in promoted["challengers"]})
            rollback = next(x for x in promoted["challengers"] if x["id"].startswith("rollback_b1_"))
            self.assertEqual(rollback["params"]["min_z"], 1.5)
            self.assertEqual(promoted["last_promotion"]["source_sha"], "abc123")


if __name__ == "__main__":
    unittest.main()
