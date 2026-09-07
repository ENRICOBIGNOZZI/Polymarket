from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/research_v7_btc_m5_external_cancel_episode_build.py"
EVALUATOR = ROOT / "scripts/research_v7_btc_m5_external_cancel_forward.py"
REGISTRY = ROOT / "config/v7_maker_fillability_experiments.json"
EXPERIMENT_ID = "btc-m5-external-cancel-overlay-forward-v1"


def rule_and_freeze() -> tuple[dict, int]:
    registry = json.loads(REGISTRY.read_text())
    exp = next(x for x in registry["experiments"] if x["experiment_id"] == EXPERIMENT_ID)
    from datetime import datetime
    freeze = int(datetime.fromisoformat(exp["start_time"].replace("Z", "+00:00")).timestamp() * 1000)
    return exp["frozen_rule"], freeze


class EpisodeBuilderTests(unittest.TestCase):
    def test_synthetic_causal_trigger_builds_avoidable_fill_and_validates(self) -> None:
        rule, freeze_ms = rule_and_freeze()
        rule_hash = hashlib.sha256(json.dumps(rule, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        base_ms = freeze_ms + 100_000
        trigger_ms = base_ms + 500
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "normalized_events").mkdir()
            protocol = {
                "canonical_rule_sha256": rule_hash,
                "canonical_rule": rule,
                "promotion_evidence_market_started_strictly_after_ms": base_ms - 10_000,
                "maker_model_published_ms": freeze_ms - 1_000,
                "maker_model_sha": "a" * 40,
                "inputs": {"source_max_age_ms": 250, "future_label_max_lag_ms": 50},
                "trigger_protocol": {"grid_ms": 25},
                "incumbent_proxy": {"quote_size_shares": 5.0, "queue_ahead_multiplier": 1.5, "primary_fill_window_ms": 500},
                "overlay": {"effective_cancel_latency_ms": 100},
                "stress": {"queue_ahead_multiplier": 3.0, "effective_cancel_latency_ms": 200},
                "labels": {"horizons_ms": [250, 500, 1000]},
            }
            (root / "episode_protocol.json").write_text(json.dumps(protocol))
            manifest = {
                "schema": "polymarket_v7_btc_m5_clob_book_session_v1",
                "payload_schema_version": 2,
                "market_id": "m1",
                "started_ms": base_ms + 1,
            }
            (root / "btc-m5-book.m1.7.manifest.json").write_text(json.dumps(manifest))
            book = root / "normalized_events" / "btc-m5-book.m1.7.segment-000000.bin"
            book.write_bytes(b"book-fixture")
            binance = root / "binance.bin"; binance.write_bytes(b"binance-fixture")
            coinbase = root / "coinbase.bin"; coinbase.write_bytes(b"coinbase-fixture")
            fake = root / "fake_dump.py"
            fake.write_text(f'''#!/usr/bin/env python3
import sys
BASE={base_ms}
mode=sys.argv[1]
if mode=="--external":
 print("seq,record_receive_mono_ns,receive_wall_ns,venue,event_type,bid,ask,bid_size,ask_size,trade_price,trade_size,trade_side,healthy")
 is_bin="binance" in sys.argv[2]
 for i in range(101):
  ms=BASE+i*25; ns=ms*1000000
  if is_bin:
   px=100.0 if i<20 else 100.004
   print(f"{{i+1}},{{ns}},{{ns}},1,2,0,0,0,0,{{px}},1,1,1")
  else:
   mid=100.0 if i<20 else 100.002
   print(f"{{i+1}},{{ns}},{{ns}},2,1,{{mid-0.5}},{{mid+0.5}},1,1,0,0,0,1")
elif mode=="--book":
 print("seq,receive_ms,receive_mono_ns,outcome,kind,bid,ask,bidq,askq,bid5,ask5,bid10,ask10,trade_price,trade_qty,trade_side")
 rows=[]
 for i in range(101):
  ms=BASE+i*25; ns=ms*1000000
  rows.append((ms,1,f"{{ms}},{{ns}},1,1,0.49,0.51,1,1,5,5,10,10,0,0,0"))
  rows.append((ms,2,f"{{ms}},{{ns}},2,1,0.49,0.51,1,1,5,5,10,10,0,0,0"))
 rows.append(({trigger_ms+150},3,f"{trigger_ms+150},{{({trigger_ms+150})*1000000}},1,2,0,0,0,0,0,0,0,0,0.51,2,1"))
 rows.sort(key=lambda x:(x[0],x[1]))
 for seq,(_,_,tail) in enumerate(rows,1): print(f"{{seq}},{{tail}}")
else:
 sys.exit(2)
''')
            fake.chmod(0o755)
            out = root / "episodes.jsonl"; summary = root / "summary.json"
            proc = subprocess.run([
                sys.executable, str(BUILDER), "--root", str(root), "--market", "m1",
                "--tape-dump", str(fake), "--external-tape", str(binance), str(coinbase),
                "--output", str(out), "--summary", str(summary),
            ], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            s = json.loads(summary.read_text())
            self.assertEqual(s["triggers"], 1)
            self.assertEqual(s["episodes"], 2)
            self.assertEqual(s["baseline_fills"], 1)
            self.assertEqual(s["overlay_fills"], 0)
            self.assertEqual(s["avoidable_fills"], 1)
            self.assertTrue(s["promotion_eligible_market"])
            self.assertEqual(s["causality_violations"], [])
            rows = [json.loads(x) for x in out.read_text().splitlines()]
            self.assertTrue(any(r["baseline_fill"] and not r["overlay_fill"] for r in rows))
            for r in rows:
                self.assertEqual(r["rule_sha256"], rule_hash)
                self.assertTrue(r["receive_time_causal"])
                self.assertFalse(r["real_order_submission"] if "real_order_submission" in r else False)
            report = root / "report.json"
            ev = subprocess.run([
                sys.executable, str(EVALUATOR), "--registry", str(REGISTRY),
                "--episodes", str(out), "--output", str(report),
            ], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(ev.returncode, 0, ev.stderr + ev.stdout)
            self.assertEqual(json.loads(report.read_text())["state"], "FORWARD_EVIDENCE_INSUFFICIENT")


if __name__ == "__main__":
    unittest.main()
