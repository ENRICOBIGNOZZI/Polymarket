from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_prepare_cutover_run_root as cutover

OLD = "a" * 40
NEW = "b" * 40
OLDER = "c" * 40


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def fixture(root: Path) -> bytes:
    write_json(root / "control/runtime_status.json", {
        "version": 7, "model_sha": OLD, "pid": 99999999,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "killed": False,
    })
    write_json(root / "control/supervisor_status.json", {"supervisor_pid": 99999998})
    (root / "control/deployed_sha").write_text(OLD + "\n")
    write_json(root / "control/portfolio_state.json", {
        "paper_only": True, "authenticated_execution": False,
        "killed": False, "drawdown": 0.01, "max_drawdown": 0.15,
    })
    ledger = (json.dumps({
        "model_sha": OLD, "paper_only": True, "authenticated_execution": False,
        "event_type": "OPPORTUNITY",
    }) + "\n").encode()
    (root / "ledger").mkdir()
    (root / "ledger/execution.jsonl").write_bytes(ledger)
    (root / "forward-tape.jsonl").write_text("preserve-me\n")
    return ledger


class V7PrepareCutoverRunRootTest(unittest.TestCase):
    def test_prior_sha_run_is_atomically_archived_with_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            archive = tmp_path / "paper_v7_archives"
            ledger = fixture(run)
            result = cutover.prepare(run, archive, tmp_path, NEW, now=123, ancestor_check=lambda *_: True)
            destination = Path(result["archive_path"])
            self.assertEqual(result["state"], "ARCHIVED_PRIOR_SHA")
            self.assertEqual((destination / "forward-tape.jsonl").read_text(), "preserve-me\n")
            self.assertEqual((destination / "ledger/execution.jsonl").read_bytes(), ledger)
            self.assertEqual(result["ledger_sha256"], hashlib.sha256(ledger).hexdigest())
            self.assertEqual(result["ledger_model_sha_counts"], {OLD: 1})
            receipt = json.loads((run / "control/cutover_lineage.json").read_text())
            self.assertEqual(receipt["previous_runtime_sha"], OLD)
            self.assertEqual(receipt["target_sha"], NEW)

    def test_same_sha_recovery_does_not_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            fixture(run)
            result = cutover.prepare(run, tmp_path / "archives", tmp_path, OLD, ancestor_check=lambda *_: True)
            self.assertEqual(result, {"state": "SAME_SHA_RECOVERY", "target_sha": OLD, "archived": False})
            self.assertTrue((run / "forward-tape.jsonl").is_file())

    def test_stopped_runtime_checkout_drift_uses_immutable_deployed_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            fixture(run)
            runtime = json.loads((run / "control/runtime_status.json").read_text())
            runtime["model_sha"] = NEW
            write_json(run / "control/runtime_status.json", runtime)
            result = cutover.prepare(
                run, tmp_path / "archives", tmp_path, NEW, now=125,
                ancestor_check=lambda *_: True,
            )
            self.assertEqual(result["previous_runtime_sha"], OLD)
            self.assertTrue(result["runtime_checkout_drift_detected"])
            self.assertEqual(result["ledger_model_sha_counts"], {OLD: 1})

    def test_live_runtime_checkout_drift_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            fixture(run)
            runtime = json.loads((run / "control/runtime_status.json").read_text())
            runtime.update({"model_sha": NEW, "pid": os.getpid()})
            write_json(run / "control/runtime_status.json", runtime)
            with self.assertRaisesRegex(cutover.CutoverArchiveError, "prior_runtime_or_supervisor_still_alive"):
                cutover.prepare(
                    run, tmp_path / "archives", tmp_path, NEW,
                    ancestor_check=lambda *_: True,
                )

    def test_prepared_lineage_only_run_root_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            archive = tmp_path / "paper_v7_archives"
            fixture(run)
            cutover.prepare(run, archive, tmp_path, NEW, now=126, ancestor_check=lambda *_: True)
            result = cutover.prepare(run, archive, tmp_path, NEW, ancestor_check=lambda *_: True)
            self.assertEqual(result, {"state": "PREPARED_RUN_ROOT", "target_sha": NEW, "archived": False})

    def test_unrelated_runtime_and_deployed_sha_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            fixture(run)
            runtime = json.loads((run / "control/runtime_status.json").read_text())
            runtime["model_sha"] = OLDER
            write_json(run / "control/runtime_status.json", runtime)
            with self.assertRaisesRegex(cutover.CutoverArchiveError, "previous_deployed_runtime_sha_mismatch"):
                cutover.prepare(
                    run, tmp_path / "archives", tmp_path, NEW,
                    ancestor_check=lambda *_: True,
                )

    def test_unsafe_prior_state_is_not_moved(self) -> None:
        for mutation, reason in (
            ("portfolio", "prior_portfolio_killed"),
            ("ledger", "ledger_sha_invalid:1"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                tmp_path = Path(directory)
                run = tmp_path / "paper_v7_live"
                fixture(run)
                if mutation == "portfolio":
                    value = json.loads((run / "control/portfolio_state.json").read_text())
                    value["killed"] = True
                    write_json(run / "control/portfolio_state.json", value)
                else:
                    write_json(run / "ledger/execution.jsonl", {
                        "model_sha": "not-a-sha", "paper_only": True, "authenticated_execution": False,
                    })
                with self.assertRaisesRegex(cutover.CutoverArchiveError, reason):
                    cutover.prepare(run, tmp_path / "archives", tmp_path, NEW, ancestor_check=lambda *_: True)
                self.assertTrue((run / "forward-tape.jsonl").is_file())

    def test_mixed_historical_ledger_shas_are_preserved_with_audited_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            archive = tmp_path / "paper_v7_archives"
            fixture(run)
            with (run / "ledger/execution.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "model_sha": OLDER, "paper_only": True, "authenticated_execution": False,
                }) + "\n")
            result = cutover.prepare(
                run, archive, tmp_path, NEW, now=124, ancestor_check=lambda *_: True,
            )
            self.assertEqual(result["ledger_model_sha_counts"], {OLD: 1, OLDER: 1})

    def test_non_ancestor_ledger_sha_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            run = tmp_path / "paper_v7_live"
            fixture(run)
            write_json(run / "ledger/execution.jsonl", {
                "model_sha": OLDER, "paper_only": True, "authenticated_execution": False,
            })
            with self.assertRaisesRegex(cutover.CutoverArchiveError, "ledger_sha_not_ancestor:1"):
                cutover.prepare(
                    run, tmp_path / "archives", tmp_path, NEW,
                    ancestor_check=lambda _root, older, _newer: older != OLDER,
                )
            self.assertTrue((run / "forward-tape.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
