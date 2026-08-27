from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7FastAuthoritySentinelContractTest(unittest.TestCase):
    def test_current_v7_authority_is_paper_only(self) -> None:
        directives = json.loads((ROOT / "config" / "operator_directives.json").read_text(encoding="utf-8"))
        auth = directives["paper_v7_authorization"]
        champion = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))

        self.assertTrue(auth["paper_only"])
        self.assertFalse(auth["authenticated_execution"])
        self.assertTrue(champion["paper_only"])
        self.assertFalse(champion["authenticated_execution"])
        self.assertFalse(champion["real_order_submission"])
        self.assertFalse(champion["legacy_fallback_allowed"])
        self.assertEqual(champion["version"], 7)
        self.assertEqual(champion["loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertFalse(auth["fixed_dollar_trade_cap_enabled"])
        self.assertEqual(auth["max_drawdown"], 0.15)

    def test_compatibility_sentinel_is_not_spendable_notional(self) -> None:
        source = (ROOT / "src" / "fast_runtime" / "part4.inc").read_text(encoding="utf-8")
        for token in (
            "capital_fraction_ceiling",
            "config.starting_capital * capital_fraction_ceiling",
            "policy.max_notional_usd > capital_ceiling",
            "policy.max_notional_usd = capital_ceiling",
            "invalid PAPER capital ceiling for fast shadow",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_deleted_fast_shadow_surfaces_stay_deleted(self) -> None:
        for rel in (
            "config/fast_arb_v7_shadow.json",
            "config/fast_arb_policy.json",
            ".github/workflows/fast-arb-hourly.yml",
        ):
            with self.subTest(path=rel):
                self.assertFalse((ROOT / rel).exists())

    def test_canonical_exact_sha_validation_replaced_shadow_workflow(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "v7-live-paper-validation.yml").read_text(encoding="utf-8")
        for token in (
            "VALIDATION_SHA",
            "ref: ${{ env.VALIDATION_SHA }}",
            'test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"',
            "Require exact-main V7 technical gates",
            "Enforce V7 PAPER safety contract",
            "Bounded same-SHA public-data PAPER runtime",
            "paper-validated",
        ):
            with self.subTest(token=token):
                self.assertIn(token, workflow)
        self.assertNotIn("fast_arb_v7_shadow.json", workflow)
        self.assertNotIn("fast-arb-hourly", workflow)


if __name__ == "__main__":
    unittest.main()
