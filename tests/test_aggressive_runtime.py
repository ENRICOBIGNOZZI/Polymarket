from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "scripts" / "sync_external_signals.py"
GUARD = ROOT / "scripts" / "aggressive_activity_guard.py"
FILTER = ROOT / "scripts" / "filter_coherent_hedges.py"
MODELS = ("micro", "pca", "graph", "semantic", "external")


class ExternalSignalBridgeTest(unittest.TestCase):
    def test_only_direct_fresh_probabilities_enter_paper_csv_and_are_shrunk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "signals.jsonl"
            output_path = root / "external.csv"
            rows = [
                {
                    "feature_name": "external_probability",
                    "market_id": "m1",
                    "observed_ts": 1000,
                    "source_event_ts": 990,
                    "q_external": 0.80,
                    "pm_mid": 0.50,
                    "confidence": 0.60,
                    "mapping_score": 0.80,
                    "source": "kalshi",
                    "source_id": "K1",
                },
                {
                    "feature_name": "return_1h",
                    "market_id": "m2",
                    "observed_ts": 1000,
                    "feature_value": 0.03,
                    "pm_mid": 0.50,
                    "confidence": 0.90,
                    "mapping_score": 1.0,
                    "source": "binance",
                },
                {
                    "feature_name": "external_probability",
                    "market_id": "stale",
                    "observed_ts": 1,
                    "source_event_ts": 1,
                    "q_external": 0.70,
                    "pm_mid": 0.50,
                    "confidence": 0.90,
                    "mapping_score": 0.90,
                    "source": "kalshi",
                },
            ]
            input_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            subprocess.run(
                [
                    sys.executable,
                    str(SYNC),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--now",
                    "1100",
                    "--max-age-seconds",
                    "600",
                    "--max-source-age-seconds",
                    "600",
                ],
                check=True,
            )
            output = list(csv.DictReader(output_path.open(newline="", encoding="utf-8")))
            self.assertEqual(len(output), 1)
            self.assertEqual(output[0]["market_key"], "m1")
            q_yes = float(output[0]["q_yes"])
            self.assertGreater(q_yes, 0.50)
            self.assertLess(q_yes, 0.80)
            self.assertIn("kalshi", output[0]["source"])

    def test_newest_best_row_wins_per_market(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "signals.jsonl"
            output_path = root / "external.csv"
            base = {
                "feature_name": "external_probability",
                "market_id": "m1",
                "source_event_ts": 995,
                "pm_mid": 0.50,
                "confidence": 0.70,
                "mapping_score": 0.80,
                "source": "kalshi",
            }
            rows = [
                {**base, "observed_ts": 1000, "q_external": 0.60, "source_id": "old"},
                {**base, "observed_ts": 1010, "q_external": 0.75, "source_id": "new"},
            ]
            input_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            subprocess.run(
                [
                    sys.executable,
                    str(SYNC),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--now",
                    "1020",
                ],
                check=True,
            )
            output = list(csv.DictReader(output_path.open(newline="", encoding="utf-8")))
            self.assertEqual(len(output), 1)
            self.assertEqual(int(output[0]["timestamp"]), 1010)
            self.assertIn("new", output[0]["source"])


class AggressiveActivityGuardTest(unittest.TestCase):
    def test_requires_all_five_models_to_scan_a_broad_tradable_universe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fields = [
                "timestamp", "name", "expert", "capital_fraction", "starting_capital",
                "cash", "equity", "pnl", "realized_pnl", "peak_equity", "drawdown",
                "gross_exposure", "open_positions", "killed", "alive",
                "status_age_seconds", "restarts", "fills", "buy_fills", "sell_fills",
                "settle_fills", "last_error",
            ]
            with (root / "strategy_status.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for name in MODELS:
                    writer.writerow(
                        {
                            "timestamp": 1000,
                            "name": name,
                            "expert": name,
                            "capital_fraction": 0.19,
                            "starting_capital": 1900,
                            "cash": 1900,
                            "equity": 1900,
                            "pnl": 0,
                            "realized_pnl": 0,
                            "peak_equity": 1900,
                            "drawdown": 0,
                            "gross_exposure": 0,
                            "open_positions": 0,
                            "killed": 0,
                            "alive": 0,
                            "status_age_seconds": 0,
                            "restarts": 0,
                            "fills": 0,
                            "buy_fills": 0,
                            "sell_fills": 0,
                            "settle_fills": 0,
                            "last_error": "",
                        }
                    )
                    log = root / "strategies" / name / "engine.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text(
                        "discovered=240 tradable=80 candidates=160 no_terminal_evidence=0\n",
                        encoding="utf-8",
                    )
                    os.utime(log, (1000, 1000))
            output = root / "activity.json"
            subprocess.run(
                [
                    sys.executable,
                    str(GUARD),
                    "--run-root",
                    str(root),
                    "--expected-models",
                    "5",
                    "--minimum-markets",
                    "150",
                    "--minimum-tradable-fraction",
                    "0.10",
                    "--max-model-staleness-seconds",
                    "60",
                    "--allow-stopped",
                    "--now",
                    "1000",
                    "--output",
                    str(output),
                ],
                check=True,
            )
            status = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(status["healthy"])
            self.assertEqual(status["total_discovered"], 1200)
            self.assertEqual(status["total_candidates"], 800)


class LatentPcaLaneTest(unittest.TestCase):
    def test_opt_in_latent_factor_lane_keeps_bounded_positive_basket(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = root / "raw.csv"
            raw.write_text(
                "market,slug,side,obs,hedges,explained,residual_z,phi,half_life_h,"
                "t_reversion,stability,hedge_error,expected_mark_move,raw_expected_edge,"
                "taker_net_edge,maker_entry_net_edge,executable_notional,legs\n"
                "1,target,YES,80,1,0.6,1.2,0.8,4,-1.1,0.5,0.5,0.02,0.02,-0.001,0.002,100,"
                "1:YES:1|2:NO:0.5\n",
                encoding="utf-8",
            )
            cache = root / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "markets": {
                            "1": {
                                "market_id": "1", "slug": "target-election", "question": "",
                                "event_id": "event-a", "category": "politics", "fetched_ts": 1000,
                            },
                            "2": {
                                "market_id": "2", "slug": "unrelated-macro", "question": "",
                                "event_id": "event-b", "category": "macro", "fetched_ts": 1000,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = root / "kept.csv"
            rejected = root / "rejected.csv"
            subprocess.run(
                [
                    sys.executable,
                    str(FILTER),
                    "--input",
                    str(raw),
                    "--output",
                    str(output),
                    "--rejections",
                    str(rejected),
                    "--cache",
                    str(cache),
                    "--allow-latent-factor",
                    "--max-latent-hedge-error",
                    "0.85",
                    "--min-latent-stability",
                    "0.20",
                    "--min-latent-z",
                    "0.65",
                    "--require-positive-maker-edge",
                    "--now",
                    "1000",
                ],
                check=True,
            )
            kept = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
            self.assertEqual(len(kept), 1)
            self.assertIn("latent_factor", kept[0]["coherence_scope"])


if __name__ == "__main__":
    unittest.main()
