from __future__ import annotations

import unittest

from scripts.v7_crypto_execution_alpha import (
    ATTRIBUTION_FIELDS,
    CancelEvidence,
    ExecutionAlphaError,
    MakerEvidence,
    MarketState,
    OutcomeBook,
    TakerEvidence,
    aggregate_attribution,
    evaluate_market,
    outcome_fair,
    select_top_markets,
)


class CryptoExecutionAlphaTests(unittest.TestCase):
    def state(
        self, *,
        fair=(0.70, 0.75, 0.80),
        yes=(0.49, 0.50, 50.0, 50.0),
        no=(0.49, 0.50, 50.0, 50.0),
        maker_mature=False,
        maker_reach=0.8,
        maker_fill_given_reach=0.8,
        maker_point_fill=0.8,
        maker_adverse=0.002,
        taker_mature=True,
        taker_fill=1.0,
        taker_slippage=0.0,
        cancel_active=False,
        cancel_mandatory=False,
        cancel_mature=False,
        cancel_probability=0.0,
        cancel_avoided_loss=0.0,
        target_size=10.0,
        market_id="m1",
    ) -> MarketState:
        return MarketState(
            market_id=market_id,
            event_id=f"e-{market_id}",
            asset="BTC",
            horizon="M5",
            fair_lower_yes=fair[0],
            fair_point_yes=fair[1],
            fair_upper_yes=fair[2],
            yes=OutcomeBook("YES", f"yes-{market_id}", yes[0], yes[1], yes[2], yes[3]),
            no=OutcomeBook("NO", f"no-{market_id}", no[0], no[1], no[2], no[3]),
            fee_schedule={"rate": 0.0, "exponent": 1.0, "takerOnly": True},
            maker=MakerEvidence(
                reach_probability_lower=maker_reach,
                fill_given_reach_probability_lower=maker_fill_given_reach,
                fill_probability_point=maker_point_fill,
                adverse_markout_upper_per_share=maker_adverse,
                toxic_fill_probability_upper=0.25,
                rebate_per_share=0.0,
                rebate_authoritative=False,
                cancel_latency_risk_per_share=0.0,
                inventory_cost_per_share=0.0,
                cancel_cost_per_quote=0.0,
                capital_cost_per_quote=0.0,
                mature=maker_mature,
            ),
            taker=TakerEvidence(
                fill_probability_lower=taker_fill,
                slippage_per_share=taker_slippage,
                latency_risk_per_share=0.0,
                unwind_loss_per_share=0.0,
                capital_cost_per_trade=0.0,
                mature=taker_mature,
            ),
            cancel=CancelEvidence(
                signal_active=cancel_active,
                mandatory_risk_cancel=cancel_mandatory,
                quote_size=5.0,
                avoidable_fill_probability_lower=cancel_probability,
                avoided_adverse_loss_lower_per_share=cancel_avoided_loss,
                cancel_cost=0.0,
                mature=cancel_mature,
            ),
            target_size=target_size,
            tte_seconds=60.0,
            settlement_verified=True,
            fair_mature=True,
            source_snapshot_identity=f"snap-{market_id}",
        )

    def assert_attribution_identity(self, report: dict) -> None:
        for candidate in report["candidates"]:
            self.assertEqual(set(candidate["attribution"]), set(ATTRIBUTION_FIELDS))
            self.assertAlmostEqual(
                candidate["conservative_expected_wealth_change"],
                sum(candidate["attribution"].values()),
                places=12,
            )

    def test_take_wins_when_settlement_lower_bound_clears_crossing_cost(self) -> None:
        report = evaluate_market(self.state())
        selected = report["selected_action"]
        self.assertEqual((selected["action"], selected["outcome"]), ("TAKE", "YES"))
        self.assertGreater(selected["conservative_expected_wealth_change"], 0.0)
        self.assertGreater(selected["attribution"]["settlement_alpha"], 0.0)
        self.assertLess(selected["attribution"]["spread_capture"], 0.0)
        self.assert_attribution_identity(report)

    def test_mature_make_can_beat_expensive_take(self) -> None:
        report = evaluate_market(self.state(
            fair=(0.54, 0.56, 0.58),
            yes=(0.50, 0.52, 50.0, 50.0),
            no=(0.48, 0.50, 50.0, 50.0),
            maker_mature=True,
            maker_adverse=0.002,
            taker_slippage=0.03,
        ))
        selected = report["selected_action"]
        self.assertEqual((selected["action"], selected["outcome"]), ("MAKE", "YES"))
        self.assertGreater(selected["attribution"]["spread_capture"], 0.0)
        self.assertLess(selected["attribution"]["adverse_selection"], 0.0)
        self.assertTrue(selected["evidence_mature"])
        self.assert_attribution_identity(report)

    def test_immature_maker_cannot_create_conservative_alpha_but_can_request_information(self) -> None:
        report = evaluate_market(self.state(
            fair=(0.51, 0.80, 0.90),
            yes=(0.49, 0.90, 50.0, 50.0),
            no=(0.09, 0.51, 50.0, 50.0),
            maker_mature=False,
            maker_point_fill=0.9,
            taker_slippage=0.02,
        ))
        self.assertEqual(report["selected_action"]["action"], "NOTHING")
        self.assertEqual(report["best_point_action"]["action"], "MAKE")
        self.assertTrue(report["maker_information_probe_recommended"])
        make = next(row for row in report["candidates"] if row["action"] == "MAKE" and row["outcome"] == "YES")
        self.assertEqual(make["expected_fill_probability"], 0.0)
        self.assertGreater(make["point_expected_wealth_change"], make["conservative_expected_wealth_change"])
        self.assertIn("MAKER_EXECUTION_EVIDENCE_IMMATURE_CONSERVATIVE_FILL_ZERO", make["reason_codes"])

    def test_mandatory_cancel_preempts_positive_alpha(self) -> None:
        report = evaluate_market(self.state(
            cancel_active=True,
            cancel_mandatory=True,
            cancel_mature=True,
            cancel_probability=0.5,
            cancel_avoided_loss=0.04,
        ))
        selected = report["selected_action"]
        self.assertEqual(selected["action"], "CANCEL")
        self.assertEqual(report["selection_reason"], "RISK_CANCEL_PREEMPTS_ALPHA")
        self.assertIn("MANDATORY_RISK_CANCEL", selected["reason_codes"])

    def test_nothing_wins_when_every_new_risk_action_is_nonpositive(self) -> None:
        report = evaluate_market(self.state(
            fair=(0.49, 0.50, 0.51),
            yes=(0.49, 0.51, 50.0, 50.0),
            no=(0.49, 0.51, 50.0, 50.0),
            maker_mature=True,
            maker_adverse=0.02,
            taker_slippage=0.02,
        ))
        self.assertEqual(report["selected_action"]["action"], "NOTHING")
        self.assertEqual(report["selection_reason"], "NO_POSITIVE_CONSERVATIVE_ACTION_VALUE")

    def test_yes_no_fair_is_exactly_complementary(self) -> None:
        state = self.state(fair=(0.21, 0.34, 0.61))
        yes = outcome_fair(state, "YES")
        no = outcome_fair(state, "NO")
        self.assertEqual(yes, (0.21, 0.34, 0.61))
        self.assertEqual(no, (0.39, 0.6599999999999999, 0.79))
        self.assertAlmostEqual(yes[0] + no[2], 1.0)
        self.assertAlmostEqual(yes[1] + no[1], 1.0)
        self.assertAlmostEqual(yes[2] + no[0], 1.0)

    def test_market_selection_keeps_only_top_conservative_opportunities(self) -> None:
        reports = [
            evaluate_market(self.state(fair=(0.52, 0.54, 0.56), market_id="a")),
            evaluate_market(self.state(fair=(0.65, 0.70, 0.75), market_id="b")),
            evaluate_market(self.state(fair=(0.80, 0.85, 0.90), market_id="c")),
            evaluate_market(self.state(fair=(0.49, 0.50, 0.51), market_id="d", taker_slippage=0.02)),
        ]
        selected = select_top_markets(reports, top_fraction=0.50)
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0]["market_id"], "c")
        self.assertNotIn("d", {row["market_id"] for row in selected})
        attribution = aggregate_attribution(selected)
        self.assertAlmostEqual(
            attribution["total_expected_wealth_change"],
            attribution["settlement_alpha"] + attribution["execution_alpha"],
        )

    def test_unauthoritative_rebate_fails_closed(self) -> None:
        state = self.state()
        bad = MarketState(**{
            **state.__dict__,
            "maker": MakerEvidence(**{
                **state.maker.__dict__,
                "rebate_per_share": 0.001,
                "rebate_authoritative": False,
            }),
        })
        with self.assertRaisesRegex(ExecutionAlphaError, "unauthoritative_rebate"):
            evaluate_market(bad)


if __name__ == "__main__":
    unittest.main()
