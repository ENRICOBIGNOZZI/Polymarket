from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from v7_retention import (
    compact_cutover_archives,
    rotate_append_reopen_streams,
    run_retention,
)

SHA = "c" * 40


class V7RuntimeRetentionTest(unittest.TestCase):
    def test_old_cutovers_keep_verified_ledger_and_drop_only_derived_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_root = Path(directory) / "paper_v7_archives"
            payloads: dict[str, bytes] = {}
            for index in range(5):
                archive = archive_root / f"cutover-{index:040x}-{1000 + index}-1"
                runtime = archive / "control/runtime_status.json"
                runtime.parent.mkdir(parents=True)
                runtime.write_text(json.dumps({
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "model_sha": f"{index:040x}",
                }))
                ledger = archive / "ledger/execution.jsonl"
                ledger.parent.mkdir(parents=True)
                payload = (json.dumps({
                    "paper_only": True,
                    "authenticated_execution": False,
                    "model_sha": f"{index:040x}",
                    "strategy": "MICRO_MAKER_PRO",
                }) + "\n").encode()
                ledger.write_bytes(payload)
                payloads[archive.name] = payload
                derived = archive / "universe/current.json"
                derived.parent.mkdir(parents=True)
                derived.write_text("derived")
                log = archive / "micro_maker/status.log"
                log.parent.mkdir(parents=True)
                log.write_text("diagnostic")
                os.utime(archive, (1000 + index, 1000 + index))

            result = compact_cutover_archives(
                archive_root, {"keep_full_generations": 2}, now=2000, dry_run=False,
            )
            self.assertEqual(len(result["protected_full_archives"]), 2)
            self.assertEqual(len(result["compacted"]), 3)
            for entry in result["compacted"]:
                archive = archive_root / entry["archive"]
                self.assertFalse((archive / "ledger/execution.jsonl").exists())
                compressed = archive / entry["ledger"]["target"]
                self.assertEqual(gzip.open(compressed, "rb").read(), payloads[archive.name])
                self.assertFalse((archive / "universe/current.json").exists())
                self.assertFalse((archive / "micro_maker/status.log").exists())
                self.assertTrue((archive / "archive/compaction_manifest.json").is_file())
            for name in result["protected_full_archives"]:
                self.assertTrue((archive_root / name / "ledger/execution.jsonl").is_file())
                self.assertTrue((archive_root / name / "universe/current.json").is_file())

    def test_only_explicit_append_reopen_streams_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tape = root / "external_fair/rtds_events.jsonl"
            tape.parent.mkdir(parents=True)
            tape.write_text("x" * 20)
            protected = root / "ledger/execution.jsonl"
            protected.parent.mkdir(parents=True)
            protected.write_text("protected")
            result = rotate_append_reopen_streams(root, {
                "append_reopen_max_bytes": 10,
                "append_reopen_streams": ["external_fair/rtds_events.jsonl"],
                "never_copytruncate": ["ledger/execution.jsonl"],
            }, now=123, dry_run=False)
            self.assertEqual(result, ["external_fair/rtds_events.jsonl.123"])
            self.assertFalse(tape.exists())
            self.assertTrue((root / result[0]).exists())
            self.assertEqual(protected.read_text(), "protected")

    def test_ledger_checkpoint_is_immutable_verified_and_source_is_never_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            ledger = run_root / "ledger/execution.jsonl"
            ledger.parent.mkdir(parents=True)
            row = {"event_type": "OPPORTUNITY", "strategy": "X", "model_sha": SHA, "paper_only": True, "authenticated_execution": False}
            original = (json.dumps(row) + "\n").encode()
            ledger.write_bytes(original)
            config = json.loads((ROOT / "config/v7_data_retention.json").read_text())
            result = run_retention(run_root, config, SHA, dry_run=False, durable_archive_confirmed=False, now=1000)
            self.assertEqual(ledger.read_bytes(), original)
            checkpoint = Path(result["ledger_checkpoint"]["path"])
            self.assertEqual(gzip.open(checkpoint, "rb").read(), original)
            self.assertEqual(result["pruned_ledger_checkpoints"], [])
            manifest = json.loads((checkpoint.parent / "manifest.json").read_text())
            self.assertEqual(manifest["checkpoints"][0]["model_sha"], SHA)
            self.assertTrue(manifest["paper_only"])

    def test_only_old_rotated_segments_expire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            ledger = run_root / "ledger/execution.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text("", encoding="utf-8")
            old = run_root / "trade_tape.csv.1"
            active = run_root / "trade_tape.csv"
            old.write_text("old", encoding="utf-8")
            active.write_text("active", encoding="utf-8")
            os.utime(old, (1, 1))
            config = json.loads((ROOT / "config/v7_data_retention.json").read_text())
            result = run_retention(run_root, config, SHA, dry_run=False, durable_archive_confirmed=False, now=40 * 86400)
            self.assertIn("trade_tape.csv.1", result["expired_rotated_segments"])
            self.assertFalse(old.exists())
            self.assertEqual(active.read_text(), "active")

    def test_unsafe_or_mixed_sha_ledger_is_never_archived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            ledger = run_root / "ledger/execution.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({"model_sha": "d" * 40, "paper_only": True, "authenticated_execution": False}) + "\n")
            config = json.loads((ROOT / "config/v7_data_retention.json").read_text())
            with self.assertRaisesRegex(ValueError, "SHA drift"):
                run_retention(run_root, config, SHA, dry_run=False, durable_archive_confirmed=False, now=1000)


if __name__ == "__main__":
    unittest.main()
