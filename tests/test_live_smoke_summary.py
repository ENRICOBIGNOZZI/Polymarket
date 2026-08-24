import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_live_smoke.py"


class LiveSmokeSummaryTest(unittest.TestCase):
    def test_snapshot_contains_metrics_candidates_intents_shadow_fillability_and_log_tails(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            run = td / "paper_v4_live"
            run.mkdir()
            shadow = run / "shadow_b1"
            shadow.mkdir()
            (run / "metrics.prom").write_text(
                'polymarket_runtime_info{adapter="v4",run_root="paper_v4_live",version="v4"} 1\n'
                'polymarket_runtime_equity_usd 10001\n'
                'polymarket_runtime_pnl_usd 1\n'
                'unrelated_metric 99\n',
                encoding="utf-8",
            )
            (run / "stat_arb_pairs_latest.log").write_text("a\nb\nc\n", encoding="utf-8")
            (run / "reward_latest.log").write_text("reward-a\nreward-b\n", encoding="utf-8")
            (run / "coherent_hedges.log").write_text(
                "coherent_hedges input=3 kept=2 rejected=1\n", encoding="utf-8"
            )
            (run / "stat_arb_pairs.csv").write_text(
                "y_market,x_market,maker_entry_net_edge\nlow,x,0.001\nhigh,y,0.009\n",
                encoding="utf-8",
            )
            (run / "stat_arb_pca_raw.csv").write_text(
                "market,raw_expected_edge,maker_entry_net_edge\np1,0.001,-0.002\np2,0.006,0.004\np3,0.02,0.01\n",
                encoding="utf-8",
            )
            (run / "stat_arb_pca.csv").write_text(
                "market,raw_expected_edge,maker_entry_net_edge,coherence_scope\np1,0.001,-0.002,semantic:0.5:3\np2,0.006,0.004,same_event:1:0\n",
                encoding="utf-8",
            )
            (run / "stat_arb_pca_rejected.csv").write_text(
                "market,raw_expected_edge,maker_entry_net_edge,coherence_reason,unrelated_market_ids\n"
                "p3,0.02,0.01,unrelated_or_unknown_hedge_legs,h9\n",
                encoding="utf-8",
            )
            (run / "reward_opportunities.csv").write_text(
                "market_id,conservative_daily_score,estimated_native_daily_value\n"
                "r-low,0.01,0.02\n"
                "r-high,0.08,0.10\n",
                encoding="utf-8",
            )
            intent_header = "bundle_id,strategy,event_id,created_ts,mode,expected_edge,max_notional,market_id,side,weight,limit_price,execution_deadline_ts,hold_deadline_ts\n"
            (run / "intents.csv").write_text(
                intent_header
                + "b1,B1,e,1,MAKER,0.006,20,m1,YES,1,0.4,2,3\n"
                + "b1,B1,e,1,MAKER,0.006,20,m2,NO,1,0.4,2,3\n",
                encoding="utf-8",
            )
            (run / "trade_tape.csv").write_text(
                "timestamp,received_ms,lag_ms,condition_id,asset_id,outcome,side,price,size,transaction_hash,slug,event_slug\n"
                "100,100000,0,c1,tokA,YES,SELL,0.40,30,tx1,s1,e1\n"
                "200,200000,0,c2,tokB,NO,SELL,0.50,40,tx2,s2,e2\n",
                encoding="utf-8",
            )
            (shadow / "stat_arb_pairs.csv").write_text(
                "y_market,x_market,maker_entry_net_edge\nshadow-y,shadow-x,0.0012\n",
                encoding="utf-8",
            )
            (shadow / "stat_arb_pairs_latest.log").write_text("shadow-summary\n", encoding="utf-8")
            (shadow / "intents.csv").write_text(
                intent_header
                + "shadow-bundle,B1,e,1,MAKER,0.0012,20,m1,YES,1,0.4,2,3\n"
                + "shadow-bundle,B1,e,1,MAKER,0.0012,20,m2,NO,1,0.5,2,3\n",
                encoding="utf-8",
            )
            (shadow / "multileg_bundles.csv").write_text(
                "bundle_id,strategy,expected_edge\nshadow-bundle,B1,0.0012\n",
                encoding="utf-8",
            )
            (shadow / "multileg_legs.csv").write_text(
                "bundle_id,market_id,side,token_id,target_shares,limit_price,queue_ahead\n"
                "shadow-bundle,m1,YES,tokA,10,0.4,50\n"
                "shadow-bundle,m2,NO,tokB,20,0.5,80\n",
                encoding="utf-8",
            )
            (run / "walk_forward.json").write_text(json.dumps({"eligible_for_tiny_pilot": False}), encoding="utf-8")
            out = td / "snapshot.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--run-root",
                    str(run),
                    "--output",
                    str(out),
                    "--git-sha",
                    "abc",
                    "--run-id",
                    "42",
                    "--tail-lines",
                    "2",
                    "--trade-lookback-seconds",
                    "400",
                ],
                check=True,
            )
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], "polymarket_public_live_smoke_v2")
            self.assertEqual(data["git_sha"], "abc")
            self.assertEqual(data["github_run_id"], "42")
            self.assertEqual(data["metrics"]["polymarket_runtime_equity_usd"], 10001.0)
            self.assertNotIn("unrelated_metric", data["metrics"])
            self.assertEqual(data["logs"]["b1"], ["b", "c"])
            self.assertEqual(data["logs"]["rewards"], ["reward-a", "reward-b"])
            self.assertEqual(data["logs"]["b2_coherence"], ["coherent_hedges input=3 kept=2 rejected=1"])
            self.assertFalse(data["walk_forward"]["eligible_for_tiny_pilot"])
            self.assertEqual(data["candidates"]["b1"][0]["y_market"], "high")
            self.assertEqual(data["candidates"]["b2"][0]["market"], "p2")
            self.assertEqual(data["candidates"]["b3_rewards"][0]["market_id"], "r-high")
            self.assertEqual(data["intents"]["rows"], 2)
            self.assertEqual(data["intents"]["bundles"], 1)
            self.assertEqual(data["intents"]["strategies"], {"B1": 2})
            self.assertEqual(data["intents"]["max_expected_edge"], 0.006)

            coherence = data["b2_coherence"]
            self.assertEqual(coherence["raw_rows"], 3)
            self.assertEqual(coherence["coherent_rows"], 2)
            self.assertEqual(coherence["rejected_rows"], 1)
            self.assertEqual(coherence["raw_positive"], 3)
            self.assertEqual(coherence["coherent_raw_positive"], 2)
            self.assertEqual(coherence["rejected_raw_positive"], 1)
            self.assertEqual(coherence["coherent_maker_positive"], 1)
            self.assertEqual(coherence["top_raw"][0]["market"], "p3")
            self.assertEqual(coherence["top_rejected"][0]["market"], "p3")

            shadow_data = data["shadow_b1"]
            self.assertEqual(shadow_data["z_threshold"], 1.25)
            self.assertEqual(shadow_data["tape_window_seconds"], 400)
            self.assertEqual(shadow_data["tape_observed_span_seconds"], 100)
            self.assertEqual(shadow_data["candidates"][0]["y_market"], "shadow-y")
            self.assertEqual(shadow_data["intents"]["bundles"], 1)
            self.assertEqual(len(shadow_data["legs"]), 2)
            self.assertAlmostEqual(shadow_data["legs"][0]["compatible_sell_volume"], 30.0)
            self.assertAlmostEqual(shadow_data["legs"][0]["estimated_queue_plus_target_clear_seconds"], 800.0)
            self.assertAlmostEqual(shadow_data["legs"][1]["estimated_queue_plus_target_clear_seconds"], 1000.0)
            self.assertTrue(shadow_data["bundles"][0]["all_legs_have_recent_compatible_flow"])
            self.assertAlmostEqual(shadow_data["bundles"][0]["max_estimated_clear_seconds"], 1000.0)


if __name__ == "__main__":
    unittest.main()
