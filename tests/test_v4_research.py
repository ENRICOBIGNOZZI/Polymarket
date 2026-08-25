#!/usr/bin/env python3
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "scripts" / "build_v4_intents.py"
MERGE = ROOT / "scripts" / "merge_v4_intents.py"
WF = ROOT / "scripts" / "walk_forward_v4.py"

FIELDS = ["bundle_id","strategy","event_id","created_ts","mode","expected_edge","max_notional","market_id","side","weight","limit_price","execution_deadline_ts","hold_deadline_ts"]
LEDGER = ["bundle_id","strategy","event_id","created_ts","closed_ts","status","expected_edge","max_notional","entry_cash","gross_pnl","fees","slippage","net_pnl","return_on_capital","fill_fraction","adverse_mark_pnl","abort_reason"]


class V4ResearchTests(unittest.TestCase):
    def test_scanner_adapter_builds_complete_b1_and_coherent_b2_bundles(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            cfg = td / "cfg.json"
            cfg.write_text(json.dumps({"max_trade_usd": 25.0}), encoding="utf-8")
            b1_scan, b1_out = td / "b1_scan.csv", td / "b1.csv"
            with b1_scan.open("w", newline="") as f:
                fields = ["y_market","x_market","half_life_h","maker_entry_net_edge","executable_notional","y_side","x_side","y_limit","x_limit","y_weight","x_weight"]
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
                w.writerow(dict(y_market="m1",x_market="m2",half_life_h=2,maker_entry_net_edge=.004,executable_notional=40,
                                y_side="YES",x_side="NO",y_limit=.41,x_limit=.38,y_weight=1,x_weight=.8))
                w.writerow(dict(y_market="bad",x_market="bad2",half_life_h=2,maker_entry_net_edge=-.01,executable_notional=40,
                                y_side="YES",x_side="NO",y_limit=.4,x_limit=.4,y_weight=1,x_weight=1))
            subprocess.run([sys.executable, str(BUILD), "--strategy", "B1", "--input", str(b1_scan), "--output", str(b1_out),
                            "--config", str(cfg), "--now", "1800000000"], check=True, capture_output=True, text=True)
            b1 = list(csv.DictReader(b1_out.open()))
            self.assertEqual(len(b1), 2)
            self.assertEqual({r["market_id"] for r in b1}, {"m1", "m2"})
            self.assertEqual({r["strategy"] for r in b1}, {"B1"})
            self.assertEqual({float(r["max_notional"]) for r in b1}, {25.0})

            b2_scan, b2_out = td / "b2_scan.csv", td / "b2.csv"
            with b2_scan.open("w", newline="") as f:
                fields = ["market","half_life_h","maker_entry_net_edge","executable_notional","legs","coherence_scope"]
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
                w.writerow(dict(market="t",half_life_h=1.5,maker_entry_net_edge=.006,executable_notional=20,
                                legs="t:NO:1|h1:YES:0.4|h2:NO:0.2",
                                coherence_scope="same_event:1.0000:0|semantic:0.5000:3"))
                # A profitable raw PCA row without a coherence certificate must never become an intent.
                w.writerow(dict(market="raw",half_life_h=1.5,maker_entry_net_edge=.02,executable_notional=100,
                                legs="raw:NO:1|unrelated:YES:0.4",coherence_scope=""))
            completed = subprocess.run([sys.executable, str(BUILD), "--strategy", "B2", "--input", str(b2_scan), "--output", str(b2_out),
                                        "--config", str(cfg), "--now", "1800000000"], check=True, capture_output=True, text=True)
            b2 = list(csv.DictReader(b2_out.open()))
            self.assertEqual(len(b2), 3)
            self.assertEqual({r["market_id"] for r in b2}, {"t", "h1", "h2"})
            self.assertEqual({r["strategy"] for r in b2}, {"B2"})
            self.assertTrue(all(float(r["limit_price"]) == 0.0 for r in b2))
            self.assertEqual(json.loads(completed.stdout)["coherence_rejected"], 1)

    def test_b2_accepts_same_category_certificate_from_upstream_gate(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scan, out = td / "scan.csv", td / "out.csv"
            with scan.open("w", newline="") as f:
                fields = ["market","half_life_h","maker_entry_net_edge","executable_notional","legs","coherence_scope"]
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
                w.writerow(dict(
                    market="target", half_life_h=8, maker_entry_net_edge=.003,
                    executable_notional=80, legs="target:NO:1|category_hedge:YES:0.4|semantic_hedge:NO:0.2",
                    coherence_scope="same_category:0.5000:1|semantic:0.1250:1",
                ))
            completed = subprocess.run([
                sys.executable, str(BUILD), "--strategy", "B2", "--input", str(scan),
                "--output", str(out), "--now", "1800000000", "--min-edge", "0.00025",
            ], check=True, capture_output=True, text=True)
            rows = list(csv.DictReader(out.open()))
            self.assertEqual(len(rows), 3)
            self.assertEqual({row["market_id"] for row in rows}, {"target", "category_hedge", "semantic_hedge"})
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["coherence_rejected"], 0)
            self.assertEqual(summary["bundles"], 1)

    def test_b2_rejects_unrelated_or_missing_target_certificate(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scan, out = td / "scan.csv", td / "out.csv"
            with scan.open("w", newline="") as f:
                fields = ["market","maker_entry_net_edge","executable_notional","legs","coherence_scope"]
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
                w.writerow(dict(market="t",maker_entry_net_edge=.02,executable_notional=100,
                                legs="other:NO:1|h:YES:1",coherence_scope="semantic:0.5:3"))
                w.writerow(dict(market="t",maker_entry_net_edge=.02,executable_notional=100,
                                legs="t:NO:1|h:YES:1",coherence_scope="unrelated:0.0:0"))
            subprocess.run([sys.executable, str(BUILD), "--strategy", "B2", "--input", str(scan), "--output", str(out),
                            "--now", "1800000000"], check=True, capture_output=True, text=True)
            self.assertEqual(list(csv.DictReader(out.open())), [])

    def test_merge_preserves_only_complete_fresh_bundles(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            a, b, out = td / "a.csv", td / "b.csv", td / "out.csv"
            now = 1_800_000_000
            with a.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
                for market, side in [("m1","YES"),("m2","NO")]:
                    w.writerow(dict(bundle_id="good",strategy="B1",event_id="e",created_ts=now-10,mode="MAKER",expected_edge=.004,max_notional=50,market_id=market,side=side,weight=1,limit_price=.4,execution_deadline_ts=now+100,hold_deadline_ts=now+500))
                # Single-leg/malformed bundle must be rejected as incomplete.
                w.writerow(dict(bundle_id="bad",strategy="B1",event_id="e",created_ts=now-10,mode="MAKER",expected_edge=.010,max_notional=50,market_id="m3",side="YES",weight=1,limit_price=.4,execution_deadline_ts=now+100,hold_deadline_ts=now+500))
            with b.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()
            subprocess.run([sys.executable, str(MERGE), "--input", str(a), "--input", str(b), "--output", str(out), "--now", str(now)], check=True, capture_output=True, text=True)
            rows = list(csv.DictReader(out.open()))
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["bundle_id"] for r in rows}, {"good"})

    def _write_ledger(self, path: Path, positive: bool):
        base = 1_700_000_000
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER); w.writeheader()
            for i in range(160):
                capital = 100.0
                # Deterministic but non-constant returns so SE/bootstrap are meaningful.
                if positive:
                    net = 0.80 + (i % 5) * 0.08
                    gross, fees, slip = net + 0.20, 0.10, 0.10
                else:
                    net = -0.40 - (i % 4) * 0.05
                    gross, fees, slip = net + 0.20, 0.10, 0.10
                created = base + i * 3600
                w.writerow(dict(bundle_id=f"b{i}", strategy="B1" if i%2==0 else "B2", event_id=f"e{i%7}", created_ts=created,
                                closed_ts=created+900, status="CLOSED", expected_edge=.005 + (i%3)*.001, max_notional=capital,
                                entry_cash=capital, gross_pnl=gross, fees=fees, slippage=slip, net_pnl=net,
                                return_on_capital=net/capital, fill_fraction=1, adverse_mark_pnl=0, abort_reason=""))

    def test_walk_forward_positive_passes_gate_and_negative_fails(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for positive in (True, False):
                ledger = td / ("pos.csv" if positive else "neg.csv")
                report = td / ("pos.json" if positive else "neg.json")
                self._write_ledger(ledger, positive)
                subprocess.run([sys.executable, str(WF), "--ledger", str(ledger), "--output", str(report),
                                "--folds", "4", "--embargo-seconds", "1800", "--min-cal-trades", "5",
                                "--min-oos-trades", "20", "--bootstrap-reps", "200", "--bootstrap-block", "4"],
                               check=True, capture_output=True, text=True)
                r = json.loads(report.read_text())
                self.assertGreater(r["oos"]["trades"], 0)
                if positive:
                    self.assertTrue(r["eligible_for_tiny_pilot"], r["gate_failures"])
                    self.assertIsNotNone(r["production_threshold"])
                    self.assertGreater(r["oos"]["net_pnl"], 0)
                    self.assertGreater(r["oos_cost_stress"]["net_pnl"], 0)
                else:
                    self.assertFalse(r["eligible_for_tiny_pilot"])
                    self.assertLess(r["oos"]["net_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
