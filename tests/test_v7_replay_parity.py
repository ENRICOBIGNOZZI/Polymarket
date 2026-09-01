from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_replay_parity", ROOT / "scripts/v7_replay_parity.py")
assert SPEC and SPEC.loader
parity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parity)

SHA = "a" * 40
SOURCE = "b" * 64


def manifest(*, observation_offset: int = 0, payload_mutation: bool = False,
             source: str = SOURCE, events: list[dict] | None = None) -> dict:
    if events is None:
        events = []
        for sequence, stage in enumerate(sorted(parity.STAGES)):
            events.append({
                "stage": stage, "sequence": sequence, "decision_cut_time_ns": 100 + sequence,
                "max_input_receive_time_ns": 90 + sequence, "reason_codes": ["CAUSAL_OK"],
                "deterministic_payload_sha256": ("c" if payload_mutation and sequence == 2 else "d") * 64,
                "observation_time_ns": 105 + sequence + observation_offset,
            })
    return parity.build_manifest(model_sha=SHA, run_id="replay-run", source_manifest_sha256=source, events=events)


class ReplayParityTests(unittest.TestCase):
    def test_identical_deterministic_path_allows_only_expected_timestamp_variance(self) -> None:
        result = parity.compare(manifest(), manifest(observation_offset=10))
        self.assertEqual(result["status"], "PARITY_OK")
        self.assertFalse(result["release_blocked"])
        self.assertEqual({row["classification"] for row in result["divergences"]}, {"EXPECTED_NONDETERMINISM"})
        self.assertEqual(result["compared_events"], len(parity.STAGES))

    def test_input_clock_and_deterministic_divergences_block_release(self) -> None:
        replay = manifest(payload_mutation=True, source="e" * 64)
        replay["events"][0]["decision_cut_time_ns"] += 1
        replay = parity.build_manifest(model_sha=SHA, run_id="replay-run", source_manifest_sha256=replay["source_manifest_sha256"], events=replay["events"])
        result = parity.compare(manifest(), replay)
        self.assertTrue(result["release_blocked"])
        kinds = {row["classification"] for row in result["divergences"]}
        self.assertTrue({"INPUT_MISSING", "CLOCK_DRIFT", "SOFTWARE_DEFECT"}.issubset(kinds))

    def test_missing_stage_or_noncausal_event_fails_before_comparison(self) -> None:
        events = manifest()["events"][:-1]
        with self.assertRaisesRegex(parity.ReplayParityError, "stage_coverage"):
            manifest(events=events)
        events = manifest()["events"]
        events[0]["max_input_receive_time_ns"] = events[0]["decision_cut_time_ns"] + 1
        with self.assertRaisesRegex(parity.ReplayParityError, "causal_cut"):
            manifest(events=events)

    def test_reports_are_immutable_and_reject_symlinks(self) -> None:
        report = parity.compare(manifest(), manifest())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            parity.immutable_write(target, report)
            parity.immutable_write(target, report)
            changed = dict(report)
            changed["status"] = "RELEASE_BLOCKED"
            with self.assertRaisesRegex(parity.ReplayParityError, "immutable_path_collision"):
                parity.immutable_write(target, changed)
            link = Path(directory) / "link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(parity.ReplayParityError, "symlink"):
                parity.immutable_write(link, report)


if __name__ == "__main__":
    unittest.main()
