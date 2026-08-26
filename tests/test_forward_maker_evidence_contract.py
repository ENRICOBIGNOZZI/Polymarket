from __future__ import annotations

import copy
import unittest

from scripts.forward_maker_evidence_contract import (
    STRICT_MARKOUT_CONTRACT,
    augment_calibration_markouts,
    compact_strict_session,
    sanitize_session_for_calibration,
)


class ForwardMakerEvidenceContractTest(unittest.TestCase):
    def raw_session(self) -> dict:
        return {
            "generated_ts": 1000,
            "git_sha": "abc",
            "github_run_id": "123",
            "results": [
                {
                    "policy": "join",
                    "any_fill": True,
                    "pair_fill": False,
                    "one_sided_only": True,
                    "conservative_pnl_ex_rewards_usd": -0.25,
                    "conditional_pnl_including_reward_usd": -0.20,
                    "matched_shares": 0.0,
                    "maker_rebate_fee_basis_usd_not_revenue": 0.0,
                    "yes": {
                        "filled_shares": 10.0,
                        "markout_45_bid_per_share": -0.01,
                        "markout_60_bid_per_share": -0.02,
                        "markout_300_bid_per_share": -0.03,
                    },
                    "no": {"filled_shares": 0.0},
                }
            ],
        }

    def test_strict_compact_tags_contract_and_carries_45s_markout(self) -> None:
        compact = compact_strict_session(self.raw_session())
        self.assertEqual(compact["markout_contract"], STRICT_MARKOUT_CONTRACT)
        summary = compact["policy_summaries"][0]
        self.assertAlmostEqual(summary["markout_45_weighted_sum"], -0.1)
        self.assertAlmostEqual(summary["markout_45_weight"], 10.0)
        self.assertAlmostEqual(summary["markout_60_weight"], 10.0)
        self.assertAlmostEqual(summary["markout_300_weight"], 10.0)

    def test_legacy_compact_markouts_are_censored_but_pnl_is_retained(self) -> None:
        compact = compact_strict_session(self.raw_session())
        compact.pop("markout_contract")
        summary = compact["policy_summaries"][0]
        original_pnl = summary["pnl_ex_rewards"]
        cleaned = sanitize_session_for_calibration(compact)
        cleaned_summary = cleaned["policy_summaries"][0]
        self.assertEqual(cleaned_summary["pnl_ex_rewards"], original_pnl)
        self.assertEqual(cleaned_summary["markout_60_weight"], 0.0)
        self.assertEqual(cleaned_summary["markout_300_weight"], 0.0)
        self.assertEqual(cleaned_summary["markout_45_weight"], 0.0)

    def test_strict_compact_markouts_are_preserved(self) -> None:
        compact = compact_strict_session(self.raw_session())
        cleaned = sanitize_session_for_calibration(compact)
        self.assertEqual(cleaned, compact)

    def test_calibration_audit_separates_strict_and_legacy_markouts(self) -> None:
        strict = compact_strict_session(self.raw_session())
        legacy = copy.deepcopy(strict)
        legacy["github_run_id"] = "legacy"
        legacy.pop("markout_contract")
        payload = {
            "history": {},
            "by_policy": {
                "join": {
                    "markout_60_observed_filled_shares": 10.0,
                    "markout_300_observed_filled_shares": 10.0,
                }
            },
        }
        result = augment_calibration_markouts(payload, [legacy, strict], 2)
        report = result["by_policy"]["join"]
        self.assertEqual(report["strict_markout_sessions"], 1)
        self.assertEqual(report["legacy_markout_sessions_excluded"], 1)
        self.assertAlmostEqual(report["filled_share_weighted_markout_45_bid_per_share"], -0.01)
        self.assertEqual(report["markout_45_observed_filled_shares"], 10.0)
        self.assertEqual(result["history"]["malformed_lines"], 2)
        self.assertTrue(result["markout_evidence_contract"]["legacy_markouts_excluded"])


if __name__ == "__main__":
    unittest.main()
