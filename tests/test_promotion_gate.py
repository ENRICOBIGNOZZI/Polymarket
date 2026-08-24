from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("promotion_gate", ROOT / "scripts" / "promotion_gate.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ALPHA_CONFIG = json.loads((ROOT / "config" / "alpha_factory.json").read_text(encoding="utf-8"))
POLICY = json.loads((ROOT / "config" / "promotion_policy.json").read_text(encoding="utf-8"))


def checks(names: tuple[str, ...]) -> list[dict]:
    return [{"__typename":"CheckRun","name":name,"status":"COMPLETED","conclusion":"SUCCESS"} for name in names]


class PromotionGateTests(unittest.TestCase):
    def fixture(self):
        source_sha = "a" * 40
        now = 1_800_000_000
        body = (
            "Source research PR/branch/commit: #10\n"
            "Promotion candidate: alpha-x\n"
            "Promotion evidence file: research/promotion_evidence/alpha-x.json\n"
        )
        candidate = {
            "number":20,"headRefName":"integration/alpha-x","headRefOid":"b"*40,
            "isDraft":False,"mergeStateStatus":"CLEAN","labels":[],
            "statusCheckRollup":checks(MODULE.REQUIRED_CANDIDATE_CHECKS),"body":body,
        }
        source = {
            "number":10,"headRefName":"research/alpha-x","headRefOid":source_sha,
            "statusCheckRollup":checks(MODULE.REQUIRED_SOURCE_CHECKS),"body":body,
            "comments":[],"reviews":[],
        }
        evidence = {
            "schema":MODULE.SCHEMA,"candidate_id":"alpha-x","source_head_sha":source_sha,
            "generated_ts":now-60,"paper_only":True,"authenticated_execution":False,
            "real_order_submission":False,"decision":"integration_ready",
            "evidence_ids":["fold-a","fold-b","fold-c"],
            "test_windows":[{"start_ts":100,"end_ts":200},{"start_ts":200,"end_ts":300},{"start_ts":300,"end_ts":400}],
            "metrics":{
                "oos_trades":40,"oos_net_pnl_usd":5.0,"stressed_1_5x_net_pnl_usd":3.0,
                "stressed_2_0x_net_pnl_usd":1.0,"max_drawdown":0.05,"profit_factor":1.30,
                "bootstrap_one_sided_pvalue":0.04,"fdr_adjusted_pvalue":0.05,"active_folds":3,
                "positive_fold_fraction":2.0/3.0,"incremental_utility":0.20,
                "single_model_compatible":True,"data_health":"healthy",
            },
        }
        return candidate, source, evidence, now

    def test_economic_candidate_passes_only_with_full_evidence(self):
        candidate, source, evidence, now = self.fixture()
        result = MODULE.evaluate(candidate, source, ["src/engine.cpp"], evidence, ALPHA_CONFIG, POLICY, now)
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["promotion_class"], "economic")

    def test_economic_candidate_without_evidence_is_blocked(self):
        candidate, source, _, now = self.fixture()
        result = MODULE.evaluate(candidate, source, ["config/paper_v5.json"], None, ALPHA_CONFIG, POLICY, now)
        self.assertFalse(result["eligible"])
        self.assertIn("economic_promotion_requires_machine_readable_evidence", result["errors"])

    def test_latest_negative_research_verdict_blocks_green_candidate(self):
        candidate, source, evidence, now = self.fixture()
        source["comments"] = [{"createdAt":"2026-08-24T22:15:30Z","body":"Research Governance — MORE_EVIDENCE_REQUIRED"}]
        result = MODULE.evaluate(candidate, source, ["src/engine.cpp"], evidence, ALPHA_CONFIG, POLICY, now)
        self.assertFalse(result["eligible"])
        self.assertIn("latest_research_verdict_blocks_promotion:MORE_EVIDENCE_REQUIRED", result["errors"])

    def test_non_overlapping_independent_evidence_is_required(self):
        candidate, source, evidence, now = self.fixture()
        evidence["test_windows"][1]["start_ts"] = 150
        result = MODULE.evaluate(candidate, source, ["src/engine.cpp"], evidence, ALPHA_CONFIG, POLICY, now)
        self.assertFalse(result["eligible"])
        self.assertIn("overlapping_test_windows", result["errors"])

    def test_operational_candidate_can_advance_without_alpha_evidence(self):
        candidate, source, _, now = self.fixture()
        candidate["body"] = "Source research PR/branch/commit: #10\n"
        source["body"] = "Operational research\n"
        result = MODULE.evaluate(candidate, source, ["docs/OPERATIONS.md"], None, ALPHA_CONFIG, POLICY, now)
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["promotion_class"], "operational")

    def test_merge_requires_controller_authorization_label(self):
        candidate, source, evidence, now = self.fixture()
        blocked = MODULE.evaluate(candidate, source, ["src/engine.cpp"], evidence, ALPHA_CONFIG, POLICY, now, require_approval_label=True)
        self.assertFalse(blocked["eligible"])
        self.assertIn("autonomous_promotion_label_missing", blocked["errors"])
        candidate["labels"] = [{"name":"autonomous-promotion-approved"}]
        allowed = MODULE.evaluate(candidate, source, ["src/engine.cpp"], evidence, ALPHA_CONFIG, POLICY, now, require_approval_label=True)
        self.assertTrue(allowed["eligible"], allowed)

    def test_candidate_code_provenance_excludes_only_live_selector(self):
        self.assertTrue(MODULE.requires_source_content_match("src/engine.cpp"))
        self.assertTrue(MODULE.requires_source_content_match("config/paper_v6.json"))
        self.assertFalse(MODULE.requires_source_content_match("config/live_champion.json"))
        self.assertFalse(MODULE.requires_source_content_match("docs/OPERATIONS.md"))


if __name__ == "__main__":
    unittest.main()
