#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import math
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
        live = json.loads((ROOT / "config/live_champion.json").read_text())
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text())
        arch = json.loads((ROOT / "config/v6_model_architecture.json").read_text())
        self.assertTrue(cfg["v6"]["paper_only"])
        self.assertTrue(cfg["multi_strategy"]["paper_only"])
        self.assertTrue(arch["paper_only"])
        self.assertFalse(arch["allow_authenticated_execution"])
        self.assertLessEqual(float(cfg["max_drawdown"]), 0.15)
        self.assertLessEqual(float(cfg["multi_strategy"]["global_max_drawdown"]), 0.15)
        self.assertLessEqual(float(cfg["max_market_fraction"]), 0.05)
        self.assertLessEqual(float(cfg["max_event_fraction"]), 0.15)
        self.assertLessEqual(float(cfg["max_gross_fraction"]), 0.70)
        self.assertLessEqual(float(cfg["multi_strategy"]["global_max_gross_fraction"]), 0.70)
        self.assertEqual(int(live["version"]), 6)
        self.assertEqual(live["loop"], "scripts/paper_v6_loop.sh")
        self.assertEqual(live["config"], "config/paper_v6.json")

    def test_capital_sleeves_are_exhaustive(self):
        v6 = json.loads((ROOT / "config/paper_v6.json").read_text())["v6"]
        total = sum(float(v6[k]) for k in (
            "micro_maker_capital_fraction", "micro_taker_capital_fraction",
            "relative_value_capital_fraction", "hard_arb_capital_fraction",
            "external_capital_fraction", "reserve_fraction",
        ))
        self.assertTrue(math.isclose(total, 1.0, abs_tol=1e-12))
        self.assertEqual(float(v6["micro_maker_capital_fraction"]), 0.22)
        self.assertEqual(float(v6["hard_arb_capital_fraction"]), 0.22)
        self.assertEqual(float(v6["reserve_fraction"]), 0.02)

    def test_semantic_is_discovery_not_fair_value(self):
        arch = json.loads((ROOT / "config/v6_model_architecture.json").read_text())
        self.assertIn("must never create a fair probability or trade", arch["semantic_role"])
        cfg = json.loads((ROOT / "config/paper_v6.json").read_text())
        self.assertEqual(float(cfg["semantic_shrink"]), 0.0)
        self.assertEqual(float(cfg["expert_weights"]["semantic"]), 0.0)

    def test_runtime_routes_each_model_to_fill_aware_execution(self):
        loop = (ROOT / "scripts/paper_v6_loop.sh").read_text()
        for token in (
            "v6_intent_guard.py", "v6_queue_filter.py", "v6_micro_maker.py",
            "v6_micro_taker_v2.py", "v6_local_factor_v3.py", "v6_hard_arb_paper_v2.py",
        ):
            self.assertIn(token, loop)
        self.assertNotIn("polymarket_pca_stat_arb", loop)
        self.assertNotIn("build_v4_intents.py --strategy B1", loop)

    def test_fee_resolver_never_promotes_unverified_fallback(self):
        common = load_script("v6_market_common_contract", "scripts/v6_market_common.py")
        details = common.FeeDetails(0.07, 1.0, True, False, "legacy_unverified_fallback")
        self.assertFalse(details.verified)
        self.assertGreater(common.fee_per_share(0.5, details, taker=True), 0.0)

    def test_fill_probability_decreases_with_queue(self):
        common = load_script("v6_market_common_fill_contract", "scripts/v6_market_common.py")
        near = common.fill_probability_proxy(
            queue_ahead=10, own_shares=10, compatible_flow_per_second=1,
            horizon_seconds=60, prior_flow_per_second=0,
        )
        far = common.fill_probability_proxy(
            queue_ahead=1000, own_shares=10, compatible_flow_per_second=1,
            horizon_seconds=60, prior_flow_per_second=0,
        )
        self.assertGreater(near, far)
        self.assertGreaterEqual(far, 0.0)

    def test_fill_probability_prior_requires_observed_compatible_flow(self):
        common = load_script("v6_market_common_inactive_prior_contract", "scripts/v6_market_common.py")
        probability = common.fill_probability_proxy(
            queue_ahead=0, own_shares=10, compatible_flow_per_second=0,
            horizon_seconds=90, prior_flow_per_second=1.0 / 300.0,
        )
        self.assertEqual(probability, 0.0)

    def test_fill_probability_prior_cannot_dominate_sparse_observed_flow(self):
        common = load_script("v6_market_common_sparse_prior_contract", "scripts/v6_market_common.py")
        observed = 0.001
        capped = common.fill_probability_proxy(
            queue_ahead=0, own_shares=10, compatible_flow_per_second=observed,
            horizon_seconds=90, prior_flow_per_second=100.0,
        )
        reference = common.fill_probability_proxy(
            queue_ahead=0, own_shares=10, compatible_flow_per_second=observed,
            horizon_seconds=90, prior_flow_per_second=observed,
        )
        self.assertAlmostEqual(capped, reference, places=15)

    def test_local_factor_v3_uses_null_preserving_level_bootstrap(self):
        lf = load_script("v6_local_factor_v3_contract", "scripts/v6_local_factor_v3.py")
        residual = [0.0]
        increments = [0.2, -0.1, 0.05, -0.08, 0.12, -0.04, 0.03, -0.07]
        for i in range(80):
            residual.append(residual[-1] + increments[i % len(increments)])
        pvalue, stat = lf.unit_root_block_pvalue(residual, seed=7, reps=80)
        self.assertTrue(math.isfinite(stat))
        self.assertGreater(pvalue, 0.0)
        self.assertLessEqual(pvalue, 1.0)
        self.assertIn("null_preserving_increment_block_adf", (ROOT / "scripts/v6_local_factor_v3.py").read_text())

    def test_micro_target_uses_last_pre_horizon_observation(self):
        target = load_script("v6_micro_target_test", "scripts/v6_micro_target.py")
        samples = [
            {"ts":100,"market_id":"m","mid":0.50,"x":[1,0,0,0,0,0],"y":None},
            {"ts":102,"market_id":"m","mid":0.51,"x":[1,0,0,0,0,0],"y":None},
            {"ts":104,"market_id":"m","mid":0.52,"x":[1,0,0,0,0,0],"y":None},
            {"ts":106,"market_id":"m","mid":0.80,"x":[1,0,0,0,0,0],"y":None},
        ]
        report = target.label_matured_samples(samples, now=106, horizon_seconds=5, max_target_staleness_seconds=2)
        self.assertAlmostEqual(samples[0]["y"], 0.02, places=12)
        self.assertEqual(samples[0]["target_observation_ts"], 104)
        self.assertEqual(report["newly_labeled"], 1)

    def test_maker_graph_hard_is_demoted_and_unverified_structural_is_blocked(self):
        now = int(time.time())
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); src, dst, status = td/"in.csv", td/"out.csv", td/"status.json"
            with src.open("w", newline="", encoding="utf-8") as h:
                w = csv.DictWriter(h, fieldnames=FIELDS); w.writeheader()
                w.writerow({"bundle_id":"g1","strategy":"GRAPH_HARD","event_id":"e","created_ts":now,"mode":"MAKER","expected_edge":0.01,"max_notional":10,"market_id":"m1","side":"YES","weight":1,"limit_price":0.4,"execution_deadline_ts":now+120,"hold_deadline_ts":now+3600})
                w.writerow({"bundle_id":"unsafe","strategy":"STRUCTURAL","event_id":"e2","created_ts":now,"mode":"MAKER","expected_edge":0.01,"max_notional":10,"market_id":"m2","side":"NO","weight":1,"limit_price":0.4,"execution_deadline_ts":now+120,"hold_deadline_ts":now+3600})
            subprocess.run([
                sys.executable, str(ROOT/"scripts/v6_intent_guard.py"), "--input", str(src),
                "--output", str(dst), "--status", str(status), "--min-edge", "0.00005",
                "--stress-bps", "5", "--max-age-seconds", "240",
            ], check=True, capture_output=True, text=True)
            with dst.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["strategy"], "GRAPH_RV")
            report = json.loads(status.read_text())
            self.assertEqual(report["rejections"]["structural_payoff_unverified"], 1)

    def test_hard_arb_v2_discovery_respects_market_scan_budget(self):
        hard_arb = load_script("v6_hard_arb_v2_pagination_test", "scripts/v6_hard_arb_paper_v2.py")
        calls = []
        original = hard_arb.request_json
        def fake_request(url, payload=None, timeout=20):
            parsed = urllib.parse.urlsplit(url); query = urllib.parse.parse_qs(parsed.query)
            offset = int(query["offset"][0]); limit = int(query["limit"][0]); calls.append((offset, limit))
            return [{"negRisk": i == 0, "liquidityNum":100.0, "eventId":f"event-{offset}"} for i in range(limit)]
        hard_arb.request_json = fake_request
        try:
            event_ids = hard_arb.discover_event_ids("https://gamma.test", 250, 10.0, 80)
        finally:
            hard_arb.request_json = original
        self.assertEqual(calls, [(0,100),(100,100),(200,50)])
        self.assertEqual(event_ids, ["event-0","event-100","event-200"])


if __name__ == "__main__":
    unittest.main()
