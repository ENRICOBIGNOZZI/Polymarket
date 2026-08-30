import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_maker_durable_learning import (  # noqa: E402
    append_new, fit_model, hazard_model, identity,
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
        self.assertAlmostEqual(fitted["groups"]["GLOBAL"]["fill_probability"], 0.4 / 28.0)
        self.assertEqual(fitted["hyperparameters"]["fill_prior_strength_orders"], 20.0)

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


if __name__ == "__main__":
    unittest.main()
