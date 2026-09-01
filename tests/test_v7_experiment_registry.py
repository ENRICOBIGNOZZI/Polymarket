from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v7_experiment_registry_test", ROOT / "scripts/v7_experiment_registry.py")
assert spec and spec.loader
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def experiment() -> dict:
    return {
        "schema": registry.SCHEMA, "experiment_id": "maker-fill-v1", "hypothesis": "queue improves fill probability",
        "primary_metric": "event_clustered_net_pnl", "secondary_metrics": ["brier_score"],
        "independent_unit": "event-condition-day", "universe_definition": "registered markets",
        "feature_cut": "receive_time<=decision_cut", "label_definition": "terminal fill outcome",
        "cost_model_version": "cost-v1", "train_period": {"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        "validation_period": {"start": "2026-02-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
        "final_holdout_period": {"start": "2026-03-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"},
        "purge": "1d", "embargo": "1d", "hyperparameter_space": {"spread": [1, 2]},
        "compute_budget": {"maximum_cpu_hours": 4, "maximum_gpu_hours": 0, "maximum_memory_bytes": 4_000_000_000}, "stopping_rule": "pre-registered boundary",
        "multiplicity_family": "maker-v1", "random_seed": 7, "code_sha": "a" * 40,
        "data_manifest": "b" * 64, "status": "REGISTERED", "result": None,
    }


class ExperimentRegistryTests(unittest.TestCase):
    def test_registry_is_immutable_and_periods_are_chronological(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = experiment()
            output = registry.immutable_register(root, value)
            self.assertEqual(output, root.resolve() / "experiments" / ("a" * 40) / "maker-fill-v1.json")
            self.assertEqual(json.loads(output.read_text()), value)
            self.assertEqual(registry.immutable_register(root, value), output)
            changed = dict(value)
            changed["hypothesis"] = "post-hoc rewrite"
            with self.assertRaisesRegex(registry.ExperimentRegistryError, "immutable_collision"):
                registry.immutable_register(root, changed)

    def test_registration_rejects_post_hoc_and_overlapping_specs(self) -> None:
        value = experiment()
        value["result"] = {"metric": 1}
        with self.assertRaisesRegex(registry.ExperimentRegistryError, "registered_experiment"):
            registry.validate(value)
        value = experiment()
        value["validation_period"] = {"start": "2026-01-15T00:00:00Z", "end": "2026-03-01T00:00:00Z"}
        with self.assertRaisesRegex(registry.ExperimentRegistryError, "not_chronological"):
            registry.validate(value)
        value = experiment()
        value["compute_budget"].pop("maximum_gpu_hours")
        with self.assertRaisesRegex(registry.ExperimentRegistryError, "compute_budget:shape"):
            registry.validate(value)


if __name__ == "__main__":
    unittest.main()
