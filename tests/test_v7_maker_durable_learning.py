import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_maker_durable_learning import (  # noqa: E402
    adverse_markout_models, append_new, fit_model, hazard_model, identity,
    placement_features,
)

SHA = "a" * 40


def record(event_type: str, record_id: str, **extra):
    value = {
        "event_type": event_type, "record_id": record_id,
        "strategy": "MICRO_MAKER_PRO", "model_sha": SHA,
        "paper_only": True, "authenticated_execution": False,
        "recorded_ts_ms": 1_000,
        "metadata": {
            "policy_hash": "policy", "config_hash": "config",
            "execution_semantics_version": "maker-paper-v7.2-bilateral-inventory",
            "outcome": "YES", "action": "JOIN", "execution_side": "BUY",
        },
    }
    value.update(extra)
    return value


class DurableLearningTests(unittest.TestCase):
    def test_append_store_deduplicates_across_cutover_sources(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = pathlib.Path(folder) / "evidence.jsonl"
            row = record("ORDER_SUBMITTED", "r1", order_id="o1", intended_size=5.0)
            self.assertEqual(append_new(store, [row, row]), (1, 1))
            self.assertEqual(append_new(store, [row]), (0, 1))
            self.assertEqual(len(store.read_text().splitlines()), 1)
            self.assertEqual(identity(row)[-1], "r1")

    def test_cancel_is_right_censored_not_identical_bernoulli(self) -> None:
        sample = [
            {"exposure_ms": 60_000, "first_fill_ms": 30_000,
             "filled_fraction": 0.5, "censored": False, "event_cluster": "a"},
            {"exposure_ms": 2_000, "first_fill_ms": None,
             "filled_fraction": 0.0, "censored": True, "event_cluster": "b"},
        ]
        model = hazard_model(sample, 0.02)
        self.assertEqual(model["censored_orders"], 1)
        self.assertGreater(model["p_any_fill_by_seconds"]["30"], 0.0)
        self.assertEqual(model["censoring_semantics"], "cancel_or_observation_end_is_right_censored")

    def test_sparse_zero_fills_shrink_without_absorbing_exploration(self) -> None:
        sample = [
            {"exposure_ms": 60_000, "first_fill_ms": None,
             "filled_fraction": 0.0, "censored": True,
             "event_cluster": f"event-{index}"}
            for index in range(8)
        ]
        model = hazard_model(sample, 0.02, 20.0)
        self.assertEqual(model["raw_expected_filled_fraction_60s"], 0.0)
        self.assertAlmostEqual(model["expected_filled_fraction_60s"], 0.4 / 28.0)
        self.assertGreater(model["expected_filled_fraction_60s"], 0.0)
        self.assertLess(model["expected_filled_fraction_60s"], 0.02)
        self.assertEqual(model["fill_prior_strength_orders"], 20.0)

        values = []
        for index in range(8):
            order_id = f"zero-fill-{index}"
            values.append(record(
                "ORDER_SUBMITTED", f"submitted-{index}", order_id=order_id,
                event_id=f"event-{index}", intended_size=2.0,
                recorded_ts_ms=1_000 + index * 1_000,
            ))
            values.append(record(
                "ORDER_CANCELLED", f"cancelled-{index}", order_id=order_id,
                event_id=f"event-{index}", order_state="CANCELLED",
                recorded_ts_ms=1_500 + index * 1_000,
            ))
        fitted = fit_model(
            values, model_sha=SHA, policy_hash="policy", config_hash="config",
            cold_fill_prior=0.02, fill_prior_strength_orders=20.0,
        )
        self.assertEqual(fitted["family"], "censored_survival_hazard_joint_cycle_v3")
        self.assertAlmostEqual(
            fitted["groups"]["GLOBAL"]["fill_probability"], 0.4 / 28.0)
        self.assertAlmostEqual(
            fitted["groups"]["GLOBAL"]["empirical_fill_probability"], 0.4 / 28.0)
        self.assertEqual(
            fitted["groups"]["GLOBAL"]["fill_probability_semantics"],
            "beta_shrunk_censored_expected_filled_fraction_per_posted_share",
        )
        self.assertEqual(fitted["hyperparameters"]["fill_prior_strength_orders"], 20.0)
        self.assertFalse(fitted["groups"]["GLOBAL"]["mature"])
        self.assertEqual(fitted["groups"]["GLOBAL"]["filled_orders"], 0)

    def test_joint_model_is_direct_and_cold_start_is_explicit(self) -> None:
        model = fit_model([], model_sha=SHA, policy_hash="policy",
                          config_hash="config", cold_fill_prior=0.02)
        self.assertEqual(model["model_state"], "COLD_START")
        self.assertEqual(model["promotion_state"], "COLD_START_CHAMPION")
        self.assertFalse(model["joint_cycle_model"]["uses_product_of_marginals"])
        self.assertEqual(model["groups"]["GLOBAL"]["fill_probability"], 0.02)
        self.assertEqual(
            model["groups"]["GLOBAL"]["fill_probability_semantics"],
            "explicit_cold_start_prior",
        )
        self.assertFalse(model["learned_placement_policy"]["valid"])
        self.assertEqual(
            model["learned_placement_policy"]["state"], "EVIDENCE_ACCUMULATING")

    def test_terminal_execution_funnel_labels_no_fill_stage(self) -> None:
        submitted = record(
            "ORDER_SUBMITTED", "submitted", order_id="o1", event_id="event-1",
            intended_size=5.0, recorded_ts_ms=1_000,
        )
        terminal = record(
            "ORDER_STATE", "terminal", order_id="o1", event_id="event-1",
            order_state="CANCELLED", recorded_ts_ms=6_000,
        )
        terminal["metadata"].update({
            "execution_outcome": "PRICE_NOT_REACHED",
            "opposite_flow_prints_seen": 3,
            "price_reach_prints_seen": 0,
            "opposite_flow_shares_seen": 12.5,
            "price_reach_shares_seen": 0.0,
        })
        fitted = fit_model(
            [submitted, terminal], model_sha=SHA, policy_hash="policy",
            config_hash="config", cold_fill_prior=0.02,
        )
        funnel = fitted["execution_funnel_labels"]
        self.assertEqual(funnel["terminal_orders"], 1)
        self.assertEqual(funnel["outcome_counts"]["PRICE_NOT_REACHED"], 1)
        self.assertEqual(funnel["opposite_flow_reach_rate"], 1.0)
        self.assertEqual(funnel["price_reach_rate"], 0.0)
        self.assertIsNone(funnel["queue_depletion_rate_given_price_reach"])

    def test_placement_features_are_side_oriented_and_complete(self) -> None:
        row = record("ORDER_SUBMITTED", "r", side="SELL")
        row["metadata"]["placement_features"] = {
            "spread_ticks": 2.0,
            "imbalance": 0.3,
            "ofi": -0.2,
            "ew_vol_ticks": 0.4,
            "trade_intensity": 0.5,
            "cancel_intensity": 0.6,
            "short_return_ticks": 0.7,
            "inventory_fraction": -0.8,
            "local_latency_ms": 0.9,
        }
        features = placement_features(row)
        self.assertIsNotNone(features)
        assert features is not None
        self.assertEqual(len(features), 10)
        self.assertEqual(features[:4], [1.0, 2.0, -0.3, 0.2])
        self.assertEqual(features[7], -0.7)
        self.assertEqual(features[8], 0.8)

    def test_incompatible_policy_is_stored_but_not_trained(self) -> None:
        row = record("ORDER_SUBMITTED", "r1", order_id="o1", intended_size=5.0)
        row["metadata"]["policy_hash"] = "old"
        model = fit_model([row], model_sha=SHA, policy_hash="policy",
                          config_hash="config", cold_fill_prior=0.02)
        self.assertEqual(model["training_window"]["records"], 0)
        self.assertEqual(model["excluded_incompatible_records"]["policy_hash"], 1)

    def test_in_sample_size_never_claims_mature_without_oos(self) -> None:
        values = []
        for index in range(60):
            order_id = f"o{index}"
            values.append(record(
                "ORDER_SUBMITTED", f"r{index}", order_id=order_id,
                event_id=f"event-{index % 15}", intended_size=5.0,
                recorded_ts_ms=1_000 + index * 100,
            ))
        model = fit_model(values, model_sha=SHA, policy_hash="policy",
                          config_hash="config", cold_fill_prior=0.02)
        self.assertEqual(model["model_state"], "EVIDENCE_ACCUMULATING")
        self.assertEqual(model["promotion_state"], "PAPER_LEARNING_CHAMPION")
        self.assertFalse(model["economically_mature"])
        self.assertIsNone(model["validation_window"])

    def test_fill_conditioned_markout_raises_risk_floor_without_promotion_credit(self) -> None:
        order = record(
            "ORDER_SUBMITTED", "order", order_id="o1", intended_size=5.0,
            intended_action="IMPROVE1", side="SELL",
        )
        fill = record("FILL", "fill", order_id="o1", filled_size=5.0)
        mark_1 = record("MARKOUT", "mark-1", order_id="o1", markouts={"1s": 0.05})
        mark_45 = record("MARKOUT", "mark-45", order_id="o1", markouts={"45s": -0.10})
        risk = adverse_markout_models([order, fill, mark_1, mark_45])
        expected = (0.002 * 20.0 + 0.10 * 5.0) / 25.0
        self.assertAlmostEqual(risk["GLOBAL"]["adverse_markout_per_share"], expected)
        self.assertEqual(risk["GLOBAL"]["adverse_markout_observations"], 1)
        self.assertEqual(risk["GLOBAL"]["adverse_markout_event_clusters"], 1)
        self.assertEqual(risk["GLOBAL"]["adverse_markout_horizon_priority"][0], "45s")

        # A prior policy cannot improve the new policy's fill estimate, but its
        # same-semantics adverse mark remains a conservative risk floor in its
        # homologous execution cell. Sparse cross-policy losses must not become
        # a universal GLOBAL floor that disables every unexplored cell.
        order["metadata"]["policy_hash"] = "old-policy"
        model = fit_model(
            [order, fill, mark_1, mark_45], model_sha=SHA,
            policy_hash="policy", config_hash="config", cold_fill_prior=0.02,
        )
        self.assertEqual(model["groups"]["GLOBAL"]["fill_probability"], 0.02)
        self.assertEqual(model["groups"]["GLOBAL"]["adverse_markout_per_share"], 0.002)
        self.assertAlmostEqual(
            model["groups"]["IMPROVE1|YES|SELL"]["adverse_markout_per_share"], expected)
        self.assertEqual(
            model["groups"]["IMPROVE1|YES|SELL"]["adverse_markout_observations"], 1)
        self.assertAlmostEqual(
            model["cross_policy_global_adverse_diagnostic"]["adverse_markout_per_share"],
            expected)
        self.assertFalse(model["groups"]["GLOBAL"]["mature"])
        self.assertGreater(model["risk_only_cross_policy_records"], 0)

    def test_placement_markout_inherits_compatibility_from_submitted_order(self) -> None:
        order = record(
            "ORDER_SUBMITTED", "order", order_id="o1", event_id="event-1",
            intended_size=5.0, intended_action="JOIN", side="SELL",
        )
        order["metadata"]["placement_features"] = {
            "spread_ticks": 2.0,
            "imbalance": 0.3,
            "ofi": -0.2,
            "ew_vol_ticks": 0.4,
            "trade_intensity": 0.5,
            "cancel_intensity": 0.6,
            "short_return_ticks": 0.7,
            "inventory_fraction": -0.8,
            "local_latency_ms": 0.9,
        }
        fill = record("FILL", "fill", order_id="o1", filled_size=5.0)
        markout = record(
            "MARKOUT", "markout", order_id="o1", markouts={"45s": -0.10})
        # Runtime lifecycle rows may omit the policy/config metadata.  Their
        # identity is inherited exclusively through the submitted order id.
        fill["metadata"] = {}
        markout["metadata"] = {}

        model = fit_model(
            [order, fill, markout], model_sha=SHA, policy_hash="policy",
            config_hash="config", cold_fill_prior=0.02,
        )

        self.assertEqual(model["groups"]["GLOBAL"]["filled_orders"], 1)
        self.assertEqual(model["learned_placement_policy"]["markout_examples"], 1)
        self.assertEqual(
            model["learned_placement_policy"]["state"], "EVIDENCE_ACCUMULATING")


if __name__ == "__main__":
    unittest.main()
