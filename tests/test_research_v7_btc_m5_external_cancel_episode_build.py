from __future__ import annotations

import hashlib,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BUILDER=ROOT/"scripts/research_v7_btc_m5_external_cancel_episode_build.py"
EVALUATOR=ROOT/"scripts/research_v7_btc_m5_external_cancel_forward.py"
REGISTRY=ROOT/"config/v7_maker_fillability_experiments.json"
EXPERIMENT_ID="btc-m5-external-cancel-overlay-forward-v1"


def frozen_experiment()->dict:
    reg=json.loads(REGISTRY.read_text())
    return next(x for x in reg["experiments"] if x["experiment_id"]==EXPERIMENT_ID)


class EpisodeBuilderTests(unittest.TestCase):
    def test_development_semantics_build_partial_avoidable_fill_and_fail_closed_lineage(self)->None:
        exp=frozen_experiment();rule=exp["frozen_rule"]
        rule_hash=hashlib.sha256(json.dumps(rule,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        from datetime import datetime
        freeze_ms=int(datetime.fromisoformat(exp["start_time"].replace("Z","+00:00")).timestamp()*1000)
        base_ms=freeze_ms+100_000;trigger_ms=base_ms+500
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/"normalized_events").mkdir()
            protocol={"schema":"polymarket_v7_btc_m5_external_cancel_episode_protocol_v2",
                "episode_schema":"polymarket_v7_btc_m5_external_cancel_forward_episode_v2",
                "canonical_rule_sha256":rule_hash,"canonical_rule":rule,
                "promotion_evidence_market_started_strictly_after_ms":base_ms-10_000,
                "maker_model_published_ms":freeze_ms-1_000,"maker_model_sha":"a"*40,
                "development_semantics":{"overlap_warmup_ms":300,"overlap_tail_ms":1200},
                "trigger_protocol":{"grid_ms":25},
                "incumbent_proxy":{"quote_size_shares":5.0,"primary_fill_window_ms":500},
                "overlay":{"effective_cancel_latency_ms":100},
                "stress":{"queue_ahead_multiplier":3.0,"effective_cancel_latency_ms":200},
                "labels":{"horizons_ms":[250,500,1000]}}
            pp=root/"protocol_v2.json";pp.write_text(json.dumps(protocol))
            manifest={"schema":"polymarket_v7_btc_m5_clob_book_session_v1","payload_schema_version":2,
                "market_id":"m1","started_ms":base_ms+1}
            (root/"btc-m5-book.m1.7.manifest.json").write_text(json.dumps(manifest))
            (root/"normalized_events"/"btc-m5-book.m1.7.segment-000000.bin").write_bytes(b"book-fixture")
            binance=root/"binance.bin";binance.write_bytes(b"binance-fixture")
            coinbase=root/"coinbase.bin";coinbase.write_bytes(b"coinbase-fixture")
            fake=root/"fake_dump.py"
            fake.write_text(f'''#!/usr/bin/env python3
import os,sys
BASE={base_ms};TRIGGER={trigger_ms};mode=sys.argv[1]
if mode=="--external":
 print("seq,record_receive_mono_ns,receive_wall_ns,venue,event_type,bid,ask,bid_size,ask_size,trade_price,trade_size,trade_side,healthy")
 is_bin="binance" in sys.argv[2]
 for i in range(201):
  ms=BASE+i*25;ns=ms*1000000
  if is_bin:
   px=100.0 if i<20 else 100.004
   print(f"{{i+1}},{{ns}},{{ns}},1,2,0,0,0,0,{{px}},1,1,1")
  else:
   mid=100.0 if i<20 else 100.002
   print(f"{{i+1}},{{ns}},{{ns}},2,1,{{mid-.5}},{{mid+.5}},1,1,0,0,0,1")
elif mode=="--book":
 print("seq,receive_ms,receive_mono_ns,outcome,kind,bid,ask,bidq,askq,bid5,ask5,bid10,ask10,trade_price,trade_qty,trade_side")
 rows=[]
 for i in range(201):
  ms=BASE+i*25;ns=ms*1000000
  quiet=os.getenv("QUIET_LABEL")=="1" and 40<=i<=44
  if not quiet:
   ybid,yask=(.52,.54) if i>=36 else (.49,.51)
   rows.append((ms,1,f"{{ms}},{{ns}},1,1,{{ybid}},{{yask}},1,1,5,5,10,10,0,0,0"))
   rows.append((ms,2,f"{{ms}},{{ns}},2,1,.49,.51,1,1,5,5,10,10,0,0,0"))
 rows.append((TRIGGER+150,3,f"{{TRIGGER+150}},{{(TRIGGER+150)*1000000}},1,2,0,0,0,0,0,0,0,0,.51,2,1"))
 rows.append((TRIGGER+250,3,f"{{TRIGGER+250}},{{(TRIGGER+250)*1000000}},1,2,0,0,0,0,0,0,0,0,.51,2,1"))
 if os.getenv("LINEAGE_GAP")=="1":
  rows.append((TRIGGER+200,0,f"{{TRIGGER+200}},{{(TRIGGER+200)*1000000}},1,4,0,0,0,0,0,0,0,0,0,0,0"))
 rows.sort(key=lambda x:(x[0],x[1]))
 for seq,(_,_,tail) in enumerate(rows,1): print(f"{{seq}},{{tail}}")
else: sys.exit(2)
''')
            fake.chmod(0o755)
            out=root/"episodes.jsonl";summary=root/"summary.json"
            env=dict(os.environ,QUIET_LABEL="1")
            proc=subprocess.run([sys.executable,str(BUILDER),"--root",str(root),"--market","m1",
                "--protocol",str(pp),"--tape-dump",str(fake),"--external-tape",str(binance),str(coinbase),
                "--output",str(out),"--summary",str(summary)],cwd=ROOT,text=True,capture_output=True,env=env)
            self.assertEqual(proc.returncode,0,proc.stderr+proc.stdout)
            s=json.loads(summary.read_text());self.assertTrue(s["market_evaluable"]);self.assertTrue(s["promotion_eligible_market"])
            self.assertEqual(s["triggers"],1);self.assertEqual(s["episodes"],2);self.assertEqual(s["avoidable_fills"],1)
            self.assertAlmostEqual(s["avoidable_filled_shares"],.5);self.assertEqual(s["stress_avoidable_fills"],1)
            self.assertAlmostEqual(s["stress_avoidable_filled_shares"],1.0);self.assertEqual(s["invalid_label_episodes"],0)
            rows=[json.loads(x) for x in out.read_text().splitlines()]
            yes=next(r for r in rows if r["research_provenance"]["outcome"]=="YES")
            self.assertTrue(yes["baseline_fill"]);self.assertFalse(yes["overlay_fill"])
            self.assertAlmostEqual(yes["baseline_filled_shares"],.5);self.assertAlmostEqual(yes["baseline_markout_per_share"]["500"],-.02)
            self.assertAlmostEqual(yes["stress"]["queue_3x_cancel_200ms"]["baseline_filled_shares"],1.0)
            report=root/"report.json"
            ev=subprocess.run([sys.executable,str(EVALUATOR),"--registry",str(REGISTRY),"--episodes",str(out),"--output",str(report)],cwd=ROOT,text=True,capture_output=True)
            self.assertEqual(ev.returncode,0,ev.stderr+ev.stdout)
            rep=json.loads(report.read_text());self.assertEqual(rep["state"],"FORWARD_EVIDENCE_INSUFFICIENT")
            self.assertAlmostEqual(rep["equal_weight_500ms_improvement_per_share"],.02)

            bad_out=root/"bad.jsonl";bad_summary=root/"bad_summary.json";env=dict(os.environ,LINEAGE_GAP="1")
            bad=subprocess.run([sys.executable,str(BUILDER),"--root",str(root),"--market","m1","--protocol",str(pp),
                "--tape-dump",str(fake),"--external-tape",str(binance),str(coinbase),"--output",str(bad_out),
                "--summary",str(bad_summary)],cwd=ROOT,text=True,capture_output=True,env=env)
            self.assertEqual(bad.returncode,0,bad.stderr+bad.stdout);bs=json.loads(bad_summary.read_text())
            self.assertFalse(bs["market_evaluable"]);self.assertIn("BOOK_CAUSALITY_VIOLATION",bs["exclusion_reason_codes"])
            self.assertEqual(bad_out.read_text(),"")


if __name__=="__main__":
    unittest.main()
