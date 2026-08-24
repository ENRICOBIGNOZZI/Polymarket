from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "filter_coherent_hedges.py"


def load_filter_module():
    spec = importlib.util.spec_from_file_location("filter_coherent_hedges_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load filter_coherent_hedges.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CoherentHedgeFilterTest(unittest.TestCase):
    def test_keeps_related_election_basket_and_rejects_cross_domain_leg(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = root / "raw.csv"
            raw.write_text(
                "market,slug,raw_expected_edge,maker_entry_net_edge,taker_net_edge,legs\n"
                "1,will-andy-beshear-win-the-2028-us-presidential-election,0.01,0.002,-0.01,1:NO:1|2:YES:0.5|3:NO:0.2\n"
                "1,will-andy-beshear-win-the-2028-us-presidential-election,0.01,0.002,-0.01,1:NO:1|4:YES:0.5\n",
                encoding="utf-8",
            )
            cache = root / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "markets": {
                            "1": {
                                "market_id": "1",
                                "slug": "will-andy-beshear-win-the-2028-us-presidential-election",
                                "question": "",
                                "event_id": "",
                                "category": "",
                                "fetched_ts": 1000,
                            },
                            "2": {
                                "market_id": "2",
                                "slug": "will-gavin-newsom-win-the-2028-us-presidential-election",
                                "question": "",
                                "event_id": "",
                                "category": "",
                                "fetched_ts": 1000,
                            },
                            "3": {
                                "market_id": "3",
                                "slug": "will-alexandria-ocasio-cortez-win-the-2028-us-presidential-election",
                                "question": "",
                                "event_id": "",
                                "category": "",
                                "fetched_ts": 1000,
                            },
                            "4": {
                                "market_id": "4",
                                "slug": "hantavirus-pandemic-in-2026",
                                "question": "",
                                "event_id": "",
                                "category": "",
                                "fetched_ts": 1000,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = root / "coherent.csv"
            rejections = root / "rejected.csv"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(raw),
                    "--output",
                    str(output),
                    "--rejections",
                    str(rejections),
                    "--cache",
                    str(cache),
                    "--now",
                    "1000",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            kept = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
            rejected = list(
                csv.DictReader(rejections.open(newline="", encoding="utf-8"))
            )
            self.assertEqual(len(kept), 1)
            self.assertEqual(len(rejected), 1)
            self.assertIn("semantic", kept[0]["coherence_scope"])
            self.assertEqual(rejected[0]["unrelated_market_ids"], "4")
            self.assertIn("kept=1 rejected=1", completed.stdout)

    def test_factor_mode_keeps_low_error_stable_pca_basket(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = root / "raw.csv"
            raw.write_text(
                "market,slug,residual_z,stability,hedge_error,maker_entry_net_edge,legs\n"
                "1,target,1.4,0.60,0.30,0.003,1:YES:1|2:NO:0.5\n",
                encoding="utf-8",
            )
            cache = root / "cache.json"
            cache.write_text(json.dumps({"markets": {
                "1": {"market_id": "1", "slug": "election-target", "question": "", "event_id": "a", "category": "", "fetched_ts": 1000},
                "2": {"market_id": "2", "slug": "crypto-hedge", "question": "", "event_id": "b", "category": "", "fetched_ts": 1000},
            }}), encoding="utf-8")
            output = root / "coherent.csv"
            rejections = root / "rejected.csv"
            subprocess.run([
                sys.executable, str(SCRIPT), "--input", str(raw), "--output", str(output),
                "--rejections", str(rejections), "--cache", str(cache), "--now", "1000",
                "--allow-factor-hedges", "--max-factor-hedge-error", "0.65",
            ], check=True, capture_output=True, text=True)
            kept = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
            self.assertEqual(len(kept), 1)
            self.assertIn("pca_factor", kept[0]["coherence_scope"])

    def test_same_event_dominates_low_text_similarity(self):
        module = load_filter_module()
        target = module.MarketMeta("1", "alpha", "", "event-7", "", 1)
        hedge = module.MarketMeta("2", "completely-different", "", "event-7", "", 1)
        scope, score, shared = module.relation(target, hedge, 0.25, 2)
        self.assertEqual(scope, "same_event")
        self.assertEqual(score, 1.0)
        self.assertEqual(shared, 0)

    def test_metadata_failure_is_fail_closed_and_erases_stale_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = root / "raw.csv"
            raw.write_text(
                "market,slug,legs\n1,target,1:YES:1|2:NO:1\n", encoding="utf-8"
            )
            cache = root / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "markets": {
                            "1": {
                                "market_id": "1",
                                "slug": "target",
                                "question": "",
                                "event_id": "",
                                "category": "",
                                "fetched_ts": 1000,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = root / "coherent.csv"
            rejections = root / "rejected.csv"
            output.write_text(
                "market,legs,coherence_scope\nstale,stale:YES:1,semantic\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(raw),
                    "--output",
                    str(output),
                    "--rejections",
                    str(rejections),
                    "--cache",
                    str(cache),
                    "--gamma-url",
                    "http://127.0.0.1:1",
                    "--timeout-seconds",
                    "0.05",
                    "--now",
                    "1000",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                list(csv.DictReader(output.open(newline="", encoding="utf-8"))), []
            )
            rejected = list(
                csv.DictReader(rejections.open(newline="", encoding="utf-8"))
            )
            self.assertEqual(rejected[0]["unrelated_market_ids"], "2")
            self.assertNotIn("stale", output.read_text(encoding="utf-8"))

    def test_missing_input_also_clears_stale_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output = root / "coherent.csv"
            rejections = root / "rejected.csv"
            output.write_text("market,legs\nstale,stale:YES:1\n", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(root / "missing.csv"),
                    "--output",
                    str(output),
                    "--rejections",
                    str(rejections),
                    "--cache",
                    str(root / "cache.json"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                list(csv.DictReader(output.open(newline="", encoding="utf-8"))), []
            )


if __name__ == "__main__":
    unittest.main()
