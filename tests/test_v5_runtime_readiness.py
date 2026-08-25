from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.v5_runtime_readiness import EXPECTED_MODELS, ReadinessError, evaluate


class V5RuntimeReadinessTest(unittest.TestCase):
    NOW = 1_000_000.0

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self._write_supervisor()
        self._write_runtime(status_age=10.0, started_at=self.NOW - 30.0)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_supervisor(self, *, ts: float | None = None, allocator_alive: str = "1") -> None:
        with (self.root / "runtime_supervisor.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "timestamp", "recorder_alive", "broker_alive", "allocator_alive",
                    "recorder_restarts", "broker_restarts", "allocator_restarts",
                    "recorder_pid", "broker_pid", "allocator_pid",
                ),
            )
            writer.writeheader()
            writer.writerow(
                {
                    "timestamp": self.NOW - 2 if ts is None else ts,
                    "recorder_alive": "1",
                    "broker_alive": "1",
                    "allocator_alive": allocator_alive,
                    "recorder_restarts": "0",
                    "broker_restarts": "0",
                    "allocator_restarts": "0",
                    "recorder_pid": "1",
                    "broker_pid": "2",
                    "allocator_pid": "3",
                }
            )

    def _write_runtime(
        self,
        *,
        status_age: float,
        started_at: float,
        model_alive: str = "1",
        allocator_ts: float | None = None,
    ) -> None:
        fractions = {"micro": 0.20, "pca": 0.25, "graph": 0.25, "semantic": 0.15, "external": 0.10}
        allocator = {
            "timestamp": self.NOW - 2 if allocator_ts is None else allocator_ts,
            "paper_only": True,
            "models_expected": 5,
            "models_alive": 5 if model_alive == "1" else 4,
            "reserve_fraction": 0.05,
        }
        (self.root / "allocator_status.json").write_text(json.dumps(allocator), encoding="utf-8")
        fields = ("timestamp", "name", "expert", "capital_fraction", "alive", "status_age_seconds")
        with (self.root / "strategy_status.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for name in sorted(EXPECTED_MODELS):
                writer.writerow(
                    {
                        "timestamp": int(self.NOW),
                        "name": name,
                        "expert": name,
                        "capital_fraction": fractions[name],
                        "alive": model_alive,
                        "status_age_seconds": status_age,
                    }
                )
        with (self.root / "allocator_events.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=("timestamp", "strategy", "expert", "event", "restart_count", "detail")
            )
            writer.writeheader()
            for name in sorted(EXPECTED_MODELS):
                writer.writerow(
                    {
                        "timestamp": started_at,
                        "strategy": name,
                        "expert": name,
                        "event": "start",
                        "restart_count": 0,
                        "detail": "",
                    }
                )

    def test_fresh_completed_model_ticks_are_ready(self) -> None:
        result = evaluate(self.root, now=self.NOW)
        self.assertEqual(result["fresh_models"], sorted(EXPECTED_MODELS))
        self.assertEqual(result["startup_grace_models"], [])

    def test_missing_first_status_is_ready_during_bounded_startup_grace(self) -> None:
        self._write_runtime(status_age=1e12, started_at=self.NOW - 90.0)
        result = evaluate(self.root, now=self.NOW)
        self.assertEqual(result["fresh_models"], [])
        self.assertEqual(result["startup_grace_models"], sorted(EXPECTED_MODELS))

    def test_stale_output_after_startup_grace_fails_closed(self) -> None:
        self._write_runtime(status_age=1e12, started_at=self.NOW - 601.0)
        with self.assertRaises(ReadinessError):
            evaluate(self.root, now=self.NOW)

    def test_dead_model_fails_even_during_startup_grace(self) -> None:
        self._write_runtime(status_age=1e12, started_at=self.NOW - 30.0, model_alive="0")
        with self.assertRaises(ReadinessError):
            evaluate(self.root, now=self.NOW)

    def test_stale_allocator_heartbeat_fails(self) -> None:
        self._write_runtime(status_age=10.0, started_at=self.NOW - 30.0, allocator_ts=self.NOW - 31.0)
        with self.assertRaises(ReadinessError):
            evaluate(self.root, now=self.NOW)


if __name__ == "__main__":
    unittest.main()