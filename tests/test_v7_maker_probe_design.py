from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v7_maker_probe_design_test", ROOT / "scripts" / "v7_maker_probe_design.py")
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)
SHA = "b" * 40


def candidate(identifier: str = "candidate-1"):
    return {"candidate_id": identifier, "received_ts_ms": 100,
            "context": {"queue_bucket": "q:0-10", "spread_bucket": "s:1",
                        "tte_bucket": "t:5m", "volatility_bucket": "v:low",
                        "activity_bucket": "a:high", "quote_lifetime_bucket": "l:1s"}}


class MakerProbeDesignTests(unittest.TestCase):
    def test_assignment_is_deterministic_pre_outcome_and_hash_chained(self) -> None:
        first = probe.assign(candidate(), experiment_id="maker-probe-v1", model_sha=SHA,
                             arms=["CONTROL", "QUEUE_PLUS"], randomization_secret="secret", minimum_size_base_units=1)
        second = probe.assign(candidate(), experiment_id="maker-probe-v1", model_sha=SHA,
                              arms=["QUEUE_PLUS", "CONTROL"], randomization_secret="secret", minimum_size_base_units=1)
        self.assertEqual(first.assignment_id, second.assignment_id)
        self.assertEqual(first.assigned_arm, second.assigned_arm)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probes.jsonl"
            sealed = probe.append_assignment(path, first)
            self.assertEqual(sealed.previous_record_hash, probe.GENESIS_HASH)
            corrupted = first.seal("a" * 64)
            with self.assertRaisesRegex(probe.ProbeError, "chain_break"):
                path.write_text(json.dumps(probe.asdict(corrupted) | {"record_kind": "PROBE_ASSIGNMENT"}) + "\n", encoding="utf-8")
                probe.append_assignment(path, first)

    def test_two_stage_calibration_is_conservative_and_paper_has_no_credit(self) -> None:
        assignment = probe.assign(candidate(), experiment_id="maker-probe-v1", model_sha=SHA,
                                  arms=["CONTROL", "QUEUE_PLUS"], randomization_secret="secret", minimum_size_base_units=10).seal(probe.GENESIS_HASH)
        values = [
            probe.outcome(assignment, mode="PAPER", terminal_ts_ms=101, flow_reached=True, filled_base_units=10, cancelled=False),
            probe.outcome(assignment, mode="PAPER", terminal_ts_ms=102, flow_reached=True, filled_base_units=0, cancelled=True),
            probe.outcome(assignment, mode="PAPER", terminal_ts_ms=103, flow_reached=False, filled_base_units=0, cancelled=True),
        ]
        report = probe.calibrate(values, minimum_terminal_per_cell=3)
        cell = report["cells"][0]
        self.assertEqual(report["state"], "PAPER_DIAGNOSTIC_ONLY")
        self.assertFalse(report["promotion_credit"])
        self.assertAlmostEqual(cell["p_flow_reaches_quote"], 2 / 3)
        self.assertAlmostEqual(cell["p_fill_given_reach"], 1 / 2)
        self.assertLess(cell["conservative_any_fill_probability"], 1 / 3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.jsonl"
            sealed = probe.append_outcome(path, values[0])
            self.assertEqual(sealed["previous_record_hash"], probe.GENESIS_HASH)
            with self.assertRaisesRegex(probe.ProbeError, "duplicate_outcome"):
                probe.append_outcome(path, values[0])

    def test_live_outcome_requires_external_evidence(self) -> None:
        assignment = probe.assign(candidate(), experiment_id="maker-probe-v1", model_sha=SHA,
                                  arms=["CONTROL", "QUEUE_PLUS"], randomization_secret="secret", minimum_size_base_units=1).seal(probe.GENESIS_HASH)
        with self.assertRaisesRegex(probe.ProbeError, "live_requires_evidence_hash"):
            probe.outcome(assignment, mode="LIVE_OBSERVED", terminal_ts_ms=101,
                          flow_reached=True, filled_base_units=1, cancelled=False)


if __name__ == "__main__":
    unittest.main()
