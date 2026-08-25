#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class V6ModelContracts(unittest.TestCase):
    def test_v6_candidate_preserves_paper_execution_separation(self):
        live=json.loads((ROOT/"config/live_champion.json").read_text());cfg=json.loads((ROOT/"config/paper_v6.json").read_text());arch=json.loads((ROOT/"config/v6_model_architecture.json").read_text())
        self.assertTrue(cfg["v6"]["paper_only"]);self.assertTrue(cfg["multi_strategy"]["paper_only"]);self.assertTrue(arch["paper_only"]);self.assertFalse(arch["allow_authenticated_execution"])
        self.assertLessEqual(float(cfg["max_drawdown"]),0.15);self.assertLessEqual(float(cfg["max_gross_fraction"]),0.45);self.assertLessEqual(float(cfg["multi_strategy"]["global_max_drawdown"]),0.15);self.assertLessEqual(float(cfg["multi_strategy"]["global_max_gross_fraction"]),0.45)
        self.assertIn(int(live["version"]),(5,6))
        if int(live["version"])==6:
            self.assertEqual(live["loop"],"scripts/paper_v6_loop.sh");self.assertEqual(live["config"],"config/paper_v6.json")
        else:
            self.assertEqual(live["loop"],"scripts/paper_v5_loop.sh");self.assertEqual(live["config"],"config/paper_v5.json")

    def test_capital_sleeves_are_exhaustive(self):
        v6=json.loads((ROOT/"config/paper_v6.json").read_text())["v6"]
        total=sum(float(v6[k]) for k in ("micro_maker_capital_fraction","micro_taker_capital_fraction","relative_value_capital_fraction","hard_arb_capital_fraction","external_capital_fraction","reserve_fraction"))
        self.assertAlmostEqual(total,1.0,places=12);self.assertGreater(float(v6["hard_arb_capital_fraction"]),0.0)

    def test_semantic_is_discovery_not_fair_value(self):
        arch=json.loads((ROOT/"config/v6_model_architecture.json").read_text());self.assertIn("must never create a fair probability or trade",arch["semantic_role"]);self.assertIn("live fair-value mixture is non-promotable",arch["scheduler_directive"]["all_schedulers"])
        cfg=json.loads((ROOT/"config/paper_v6.json").read_text());self.assertEqual(float(cfg["semantic_shrink"]),0.0);self.assertEqual(float(cfg["expert_weights"]["semantic"]),0.0)

    def test_runtime_routes_each_model_to_its_execution(self):
        loop=(ROOT/"scripts/paper_v6_loop.sh").read_text()
        self.assertIn("v6_intent_guard.py",loop);self.assertIn("relation_intents_raw.csv",loop);self.assertIn("v6_micro_taker_institutional.py",loop);self.assertIn("v6_dynamic_factor_intents.py",loop);self.assertIn("v6_hard_arb_paper.py",loop);self.assertIn("polymarket_maker_paper",loop);self.assertIn("v6_global_risk.py",loop)
        self.assertNotIn("polymarket_pca_stat_arb",loop);self.assertNotIn("build_v4_intents.py --strategy B1",loop)

    def test_micro_target_uses_last_pre_horizon_observation(self):
        target=load_script("v6_micro_target_test","scripts/v6_micro_target.py")
        samples=[
            {"ts":100,"market_id":"m","mid":0.50,"x":[1,0,0,0,0,0],"y":None},
            {"ts":102,"market_id":"m","mid":0.51,"x":[1,0,0,0,0,0],"y":None},
            {"ts":104,"market_id":"m","mid":0.52,"x":[1,0,0,0,0,0],"y":None},
            {"ts":106,"market_id":"m","mid":0.80,"x":[1,0,0,0,0,0],"y":None},
        ]
        report=target.label_matured_samples(samples,now=106,horizon_seconds=5,max_target_staleness_seconds=2)
        self.assertAlmostEqual(samples[0]["y"],0.02,places=12)
        self.assertEqual(samples[0]["target_observation_ts"],104)
        self.assertEqual(samples[0]["target_staleness_seconds"],1)
        self.assertNotAlmostEqual(samples[0]["y"],0.30,places=12)
        self.assertEqual(report["newly_labeled"],1)

    def test_micro_target_requires_post_origin_observation_and_staleness_bound(self):
        target=load_script("v6_micro_target_stale_test","scripts/v6_micro_target.py")
        no_future=[{"ts":100,"market_id":"m","mid":0.50,"x":[1,0,0,0,0,0],"y":None}]
        report=target.label_matured_samples(no_future,now=106,horizon_seconds=5,max_target_staleness_seconds=5)
        self.assertIsNone(no_future[0]["y"]);self.assertEqual(report["missing_pre_horizon_observation"],1)
        stale=[
            {"ts":100,"market_id":"m","mid":0.50,"x":[1,0,0,0,0,0],"y":None},
            {"ts":101,"market_id":"m","mid":0.51,"x":[1,0,0,0,0,0],"y":None},
        ]
        report=target.label_matured_samples(stale,now=106,horizon_seconds=5,max_target_staleness_seconds=2)
        self.assertIsNone(stale[0]["y"]);self.assertEqual(report["stale_pre_horizon_observation"],1)

    def test_micro_runtime_no_longer_uses_first_post_horizon_mark(self):
        text=(ROOT/"scripts/v6_micro_taker.py").read_text()
        self.assertIn("label_matured_samples",text)
        self.assertIn("max-target-staleness-seconds",text)
        self.assertNotIn("first observable mark after the forecast horizon",text)
        self.assertIn("realized_pnl_total",text)

    def test_maker_graph_hard_is_demoted_and_unverified_structural_is_blocked(self):
        now=int(time.time())
        with tempfile.TemporaryDirectory() as td:
            td=Path(td);src,dst,status=td/"in.csv",td/"out.csv",td/"status.json"
            with src.open("w",newline="",encoding="utf-8") as h:
                w=csv.DictWriter(h,fieldnames=FIELDS);w.writeheader()
                w.writerow({"bundle_id":"g1","strategy":"GRAPH_HARD","event_id":"e","created_ts":now,"mode":"MAKER","expected_edge":0.01,"max_notional":10,"market_id":"m1","side":"YES","weight":1,"limit_price":0.4,"execution_deadline_ts":now+120,"hold_deadline_ts":now+3600})
                w.writerow({"bundle_id":"unsafe","strategy":"STRUCTURAL","event_id":"e2","created_ts":now,"mode":"MAKER","expected_edge":0.01,"max_notional":10,"market_id":"m2","side":"NO","weight":1,"limit_price":0.4,"execution_deadline_ts":now+120,"hold_deadline_ts":now+3600})
            subprocess.run([sys.executable,str(ROOT/"scripts/v6_intent_guard.py"),"--input",str(src),"--output",str(dst),"--status",str(status),"--min-edge","0.0002","--stress-bps","10","--max-age-seconds","240"],check=True,capture_output=True,text=True)
            with dst.open(newline="",encoding="utf-8") as handle:
                rows=list(csv.DictReader(handle))
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]["strategy"],"GRAPH_RV")
            report=json.loads(status.read_text());self.assertEqual(report["relabeled_graph_hard_to_rv"],1);self.assertEqual(report["rejections"]["structural_payoff_unverified"],1);self.assertFalse(report["structural_enabled"])

    def test_hard_arb_executor_requires_complete_same_snapshot_depth(self):
        text=(ROOT/"scripts/v6_hard_arb_paper.py").read_text()
        self.assertIn("negRiskAugmented",text);self.assertIn("all-or-none",text.lower());self.assertIn("min_size",text);self.assertIn("cost_per_share",text);self.assertIn("fee_ps",text)

    def test_hard_arb_discovery_respects_market_scan_budget(self):
        hard_arb=load_script("v6_hard_arb_pagination_test","scripts/v6_hard_arb_paper.py")
        calls=[]
        original=hard_arb.get_json

        def fake_get_json(url, payload=None, timeout=20):
            parsed=urllib.parse.urlsplit(url)
            query=urllib.parse.parse_qs(parsed.query)
            offset=int(query["offset"][0]);limit=int(query["limit"][0])
            calls.append((offset,limit))
            rows=[]
            for i in range(limit):
                rows.append({
                    "negRisk": i == 0,
                    "liquidityNum": 100.0,
                    "eventId": f"event-{offset}",
                })
            return rows

        hard_arb.get_json=fake_get_json
        try:
            event_ids=hard_arb.discover_event_ids("https://gamma.test",250,10.0,80)
        finally:
            hard_arb.get_json=original

        self.assertEqual(calls,[(0,100),(100,100),(200,50)])
        self.assertEqual(event_ids,["event-0","event-100","event-200"])


if __name__=="__main__":unittest.main()
