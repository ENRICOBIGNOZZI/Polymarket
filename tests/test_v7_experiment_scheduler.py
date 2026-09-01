from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


registry = load("v7_experiment_registry")
scheduler = load("v7_experiment_scheduler")


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
        "compute_budget": {"maximum_cpu_hours": 1, "maximum_gpu_hours": 0, "maximum_memory_bytes": 1000},
        "stopping_rule": "pre-registered boundary", "multiplicity_family": "maker-v1", "random_seed": 7,
        "code_sha": "a" * 40, "data_manifest": "b" * 64, "status": "REGISTERED", "result": None,
    }


def run(spec: dict, *, attempt: int, status: str = "FAILED", stopping: str = "FAILURE",
        resume: str | None = None, cpu_seconds: int = 1) -> dict:
    return scheduler.build_run(
        experiment=spec, attempt=attempt, started_at="2026-04-01T00:00:00Z", ended_at="2026-04-01T00:00:10Z",
        resource_usage={"cpu_seconds": cpu_seconds, "gpu_seconds": 0, "peak_memory_bytes": 100},
        cached_intermediates=[{"name": "features", "sha256": "c" * 64}], stopping_condition=stopping,
        status=status, failure_reason="interrupted" if status == "FAILED" else None,
        output_hashes=[{"name": "metrics", "sha256": "d" * 64}] if status == "COMPLETED" else [],
        resume_of_run_sha256=resume,
    )


class ExperimentSchedulerTests(unittest.TestCase):
    def test_failed_run_resumes_with_same_immutable_experiment_identity(self) -> None:
        spec = experiment()
        first = run(spec, attempt=1)
        second = run(spec, attempt=2, status="COMPLETED", stopping="COMPLETED", resume=first["run_sha256"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = scheduler.immutable_record(root, first, experiment=spec)
            self.assertEqual(scheduler.immutable_record(root, first, experiment=spec), first_path)
            second_path = scheduler.immutable_record(root, second, experiment=spec)
            self.assertEqual(second_path.name, "0002.json")
            self.assertEqual(scheduler._existing_runs(root, spec)[1]["run_sha256"], second["run_sha256"])

    def test_budget_and_resume_chain_fail_closed(self) -> None:
        spec = experiment()
        with self.assertRaisesRegex(scheduler.ExperimentSchedulerError, "compute_budget_exceeded"):
            run(spec, attempt=1, cpu_seconds=3601)
        first = run(spec, attempt=1)
        second = run(spec, attempt=2, resume="e" * 64)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler.immutable_record(root, first, experiment=spec)
            with self.assertRaisesRegex(scheduler.ExperimentSchedulerError, "resume_chain"):
                scheduler.immutable_record(root, second, experiment=spec)

    def test_completed_experiment_and_post_hoc_identity_change_cannot_resume(self) -> None:
        spec = experiment()
        first = run(spec, attempt=1, status="COMPLETED", stopping="COMPLETED")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scheduler.immutable_record(root, first, experiment=spec)
            second = run(spec, attempt=2, resume=first["run_sha256"])
            with self.assertRaisesRegex(scheduler.ExperimentSchedulerError, "completed_experiment"):
                scheduler.immutable_record(root, second, experiment=spec)
            altered = dict(spec)
            altered["random_seed"] = 8
            with self.assertRaisesRegex(scheduler.ExperimentSchedulerError, "run:identity"):
                scheduler.validate_run(first, experiment=altered)


if __name__ == "__main__":
    unittest.main()
