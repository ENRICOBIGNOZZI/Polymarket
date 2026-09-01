from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = load("v7_maker_probe_design")
support = load("v7_simulator_calibration_support")
SHA = "b" * 40


def candidate(identifier: str):
    return {"candidate_id": identifier, "received_ts_ms": 100,
            "context": {"queue_bucket": "q:0-10", "spread_bucket": "s:1", "tte_bucket": "t:5m",
                        "volatility_bucket": "v:low", "activity_bucket": "a:high", "quote_lifetime_bucket": "l:1s"}}


def live_calibration() -> tuple[dict, dict]:
    assignments, prior = [], probe.GENESIS_HASH
    outcomes = []
    for index in range(3):
        assignment = probe.assign(candidate(f"candidate-{index}"), experiment_id="maker-probe-v1", model_sha=SHA,
                                  arms=["CONTROL", "QUEUE_PLUS"], randomization_secret="seed",
                                  minimum_size_base_units=1).seal(prior)
        prior = assignment.record_hash
        assignments.append(assignment)
        outcomes.append(probe.outcome(assignment, mode="LIVE_OBSERVED", terminal_ts_ms=101 + index,
                                      flow_reached=True, filled_base_units=1, cancelled=False,
                                      evidence_record_hash=(str(index) * 64)[:64]))
    return probe.calibrate(outcomes, assignments=assignments, minimum_terminal_per_cell=3), candidate("x")["context"]


class SimulatorCalibrationSupportTests(unittest.TestCase):
    def test_only_mature_live_observed_exact_cell_supports_simulation(self) -> None:
        calibration, context = live_calibration()
        result = support.decide(calibration, context=context)
        self.assertTrue(result["simulation_supported"])
        self.assertFalse(result["live_execution_authorized"])
        self.assertGreater(result["conservative_any_fill_probability"], 0)

    def test_paper_or_out_of_support_context_fails_closed(self) -> None:
        calibration, context = live_calibration()
        calibration["state"] = "PAPER_DIAGNOSTIC_ONLY"
        calibration["calibration_sha256"] = support.digest({key: value for key, value in calibration.items() if key != "calibration_sha256"})
        result = support.decide(calibration, context=context)
        self.assertFalse(result["simulation_supported"])
        self.assertIn("calibration_not_live_observed", result["reason_codes"])
        calibration, context = live_calibration()
        context["spread_bucket"] = "s:2"
        result = support.decide(calibration, context=context)
        self.assertFalse(result["simulation_supported"])
        self.assertIn("context_outside_calibrated_support", result["reason_codes"])

    def test_mixed_model_calibration_is_rejected(self) -> None:
        first = probe.assign(candidate("a"), experiment_id="maker-probe-v1", model_sha=SHA,
                             arms=["CONTROL", "QUEUE_PLUS"], randomization_secret="seed", minimum_size_base_units=1).seal(probe.GENESIS_HASH)
        second = probe.assign(candidate("b"), experiment_id="maker-probe-v1", model_sha="c" * 40,
                              arms=["CONTROL", "QUEUE_PLUS"], randomization_secret="seed", minimum_size_base_units=1).seal(first.record_hash)
        with self.assertRaisesRegex(probe.ProbeError, "mixed_experiment_or_model_sha"):
            probe.calibrate([], assignments=[first, second])


if __name__ == "__main__":
    unittest.main()
