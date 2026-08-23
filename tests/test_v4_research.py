#!/usr/bin/env python3
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MERGE = ROOT / "scripts" / "merge_v4_intents.py"
WF = ROOT / "scripts" / "walk_forward_v4.py"

FIELDS = ["bundle_id","strategy","event_id","created_ts","mode","expected_edge","max_notional","market_id","side","weight","limit_price","execution_deadline_ts","hold_deadline_ts"]
LEDGER = ["bundle_id","strategy","event_id","created_ts","closed_ts","status","expected_edge","max_notional","entry_cash","gross_pnl","fees","slippage","net_pnl","return_on_capital","fill_fraction","adverse_mark_pnl","abort_reason"]


class V4ResearchTests(unittest.TestCase):
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
