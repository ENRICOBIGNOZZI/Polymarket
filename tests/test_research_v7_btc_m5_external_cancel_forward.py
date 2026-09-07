from __future__ import annotations

import importlib.util,json,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULE_PATH=ROOT/"scripts/research_v7_btc_m5_external_cancel_forward.py"
SPEC=importlib.util.spec_from_file_location("research_v7_btc_m5_external_cancel_forward",MODULE_PATH)
assert SPEC and SPEC.loader
forward=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(forward)
REGISTRY=ROOT/"config/v7_maker_fillability_experiments.json"
MODEL_SHA="a"*40


class ExternalCancelForwardEvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls)->None:
        experiment=forward.load_experiment(REGISTRY,forward.DEFAULT_EXPERIMENT_ID)
        cls.rule_hash=forward.canonical_rule_hash(experiment["frozen_rule"])
        cls.freeze_ms=forward.utc_ms(experiment["start_time"])

    def episode(self,market:int,index:int,*,primary_improvement:float=.10,stress_improvement:float=.05,
                avoid_fill:bool=True,fill_qty:float=5.0,stress_qty:float=5.0)->dict:
        quote_ms=self.freeze_ms+1_000+market*10_000+index
        baseline_500=-primary_improvement;overlay_fill=not avoid_fill
        bmarks={"250":baseline_500*.8,"500":baseline_500,"1000":baseline_500*.7}
        omarks=dict(bmarks) if overlay_fill else {}
        stress_baseline=-stress_improvement
        return {"schema":forward.EPISODE_SCHEMA,"market_id":f"m{market:03d}","quote_id":f"m{market:03d}-q{index}",
            "quote_receive_ms":quote_ms,"maker_model_published_ms":self.freeze_ms-1_000,"maker_model_sha":MODEL_SHA,
            "rule_sha256":self.rule_hash,"book_tape_schema":2,"receive_time_causal":True,"causality_violations":[],
            "trigger_applied":True,"quote_size_shares":5.0,"baseline_fill":True,"overlay_fill":overlay_fill,
            "baseline_filled_shares":fill_qty,"overlay_filled_shares":fill_qty if overlay_fill else 0.0,
            "baseline_markout_per_share":bmarks,"overlay_markout_per_share":omarks,
            "stress":{forward.STRESS_KEY:{"baseline_fill":True,"overlay_fill":False,
                "baseline_filled_shares":stress_qty,"overlay_filled_shares":0.0,
                "baseline_markout_per_share":{"500":stress_baseline},"overlay_markout_per_share":{}}}}

    @staticmethod
    def nofill(row:dict)->dict:
        row=dict(row);row.update({"baseline_fill":False,"overlay_fill":False,"baseline_filled_shares":0.0,
            "overlay_filled_shares":0.0,"baseline_markout_per_share":{},"overlay_markout_per_share":{}})
        row["stress"]={forward.STRESS_KEY:{"baseline_fill":False,"overlay_fill":False,
            "baseline_filled_shares":0.0,"overlay_filled_shares":0.0,
            "baseline_markout_per_share":{},"overlay_markout_per_share":{}}}
        return row

    def run_rows(self,rows:list[dict])->dict:
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"episodes.jsonl"
            path.write_text("".join(json.dumps(row)+"\n" for row in rows),encoding="utf-8")
            return forward.evaluate(REGISTRY,[path])

    def test_insufficient_forward_evidence_is_not_failure_or_pass(self)->None:
        report=self.run_rows([self.episode(0,0)])
        self.assertEqual(report["state"],"FORWARD_EVIDENCE_INSUFFICIENT")
        self.assertIn("INSUFFICIENT_INDEPENDENT_MARKETS",report["reason_codes"])
        self.assertIn("INSUFFICIENT_AVOIDABLE_FILL_EVENTS",report["reason_codes"])

    def test_complete_positive_forward_panel_passes_all_frozen_gates(self)->None:
        rows=[self.episode(market,index) for market in range(30) for index in range(2)]
        report=self.run_rows(rows)
        self.assertEqual(report["state"],"PASS");self.assertEqual(report["market_count"],30)
        self.assertEqual(report["market_count_with_avoidable_fills"],30)
        self.assertEqual(report["avoidable_fill_events"],60);self.assertEqual(report["avoidable_filled_shares"],300.0)
        self.assertAlmostEqual(report["equal_weight_500ms_improvement_per_share"],.10)
        self.assertGreater(report["leave_best_market_out_500ms_improvement_per_share"],0.0)
        self.assertEqual(report["positive_market_fraction"],1.0)
        self.assertAlmostEqual(report["stress_3x_queue_200ms_cancel_improvement_per_share"],.05)
        self.assertEqual(report["reason_codes"],[])

    def test_negative_stress_fails_after_minimum_evidence_is_met(self)->None:
        rows=[self.episode(market,index,stress_improvement=-.10) for market in range(30) for index in range(2)]
        report=self.run_rows(rows)
        self.assertEqual(report["state"],"FAIL")
        self.assertIn("STRESS_3X_QUEUE_200MS_CANCEL_NOT_POSITIVE",report["reason_codes"])
        self.assertLess(report["stress_3x_queue_200ms_cancel_improvement_per_share"],0.0)

    def test_fill_conditioned_metric_is_not_diluted_by_nonfills(self)->None:
        rows=[self.episode(0,0,primary_improvement=.12)]
        rows.extend(self.nofill(self.episode(0,i+1)) for i in range(20))
        report=self.run_rows(rows)
        self.assertEqual(report["market_count"],1);self.assertEqual(report["market_count_with_avoidable_fills"],1)
        self.assertEqual(report["avoidable_fill_events"],1)
        self.assertAlmostEqual(report["equal_weight_500ms_improvement_per_share"],.12)

    def test_partial_fills_are_share_weighted_within_market(self)->None:
        rows=[self.episode(0,0,primary_improvement=.10,fill_qty=1.0),
              self.episode(0,1,primary_improvement=.20,fill_qty=4.0)]
        report=self.run_rows(rows)
        self.assertAlmostEqual(report["markets"][0]["primary_500ms_improvement_per_share"],.18)
        self.assertAlmostEqual(report["markets"][0]["avoidable_filled_shares"],5.0)

    def test_prefreeze_and_model_lookahead_are_rejected(self)->None:
        row=self.episode(0,0);row["quote_receive_ms"]=self.freeze_ms
        with self.assertRaisesRegex(forward.EvidenceError,"not_strictly_forward"):
            self.run_rows([row])
        row=self.episode(0,0);row["maker_model_published_ms"]=row["quote_receive_ms"]
        with self.assertRaisesRegex(forward.EvidenceError,"maker_model_lookahead"):
            self.run_rows([row])

    def test_rule_drift_overlay_creation_and_quantity_drift_fail_closed(self)->None:
        row=self.episode(0,0);row["rule_sha256"]="0"*64
        with self.assertRaisesRegex(forward.EvidenceError,"rule_hash_drift"):
            self.run_rows([row])
        row=self.episode(0,0);row.update({"baseline_fill":False,"baseline_filled_shares":0.0,
            "baseline_markout_per_share":{},"overlay_fill":True,"overlay_filled_shares":5.0,
            "overlay_markout_per_share":{"250":0.0,"500":0.0,"1000":0.0}})
        with self.assertRaisesRegex(forward.EvidenceError,"overlay_created_fill"):
            self.run_rows([row])
        row=self.episode(0,0);row["baseline_filled_shares"]=6.0
        with self.assertRaisesRegex(forward.EvidenceError,"baseline_filled_shares_range"):
            self.run_rows([row])

    def test_pre_cancel_overlay_cannot_change_same_fill(self)->None:
        row=self.episode(0,0,avoid_fill=False);row["overlay_filled_shares"]=4.0
        with self.assertRaisesRegex(forward.EvidenceError,"overlay_changed_pre_cancel_fill_quantity"):
            self.run_rows([row])
        row=self.episode(0,0,avoid_fill=False);row["overlay_markout_per_share"]["500"]+=.01
        with self.assertRaisesRegex(forward.EvidenceError,"overlay_changed_pre_cancel_markout"):
            self.run_rows([row])

    def test_duplicate_episode_identity_and_v1_schema_are_rejected(self)->None:
        row=self.episode(0,0)
        with self.assertRaisesRegex(forward.EvidenceError,"duplicate_episode"):
            self.run_rows([row,dict(row)])
        row=self.episode(0,0);row["schema"]="polymarket_v7_btc_m5_external_cancel_forward_episode_v1"
        with self.assertRaisesRegex(forward.EvidenceError,"episode_schema"):
            self.run_rows([row])


if __name__=="__main__":
    unittest.main()
