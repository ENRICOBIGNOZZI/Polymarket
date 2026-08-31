from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from v7_runtime_contract import (
    MAKER_ROTATION_OPERATIONAL_STATES,
    RECOVERABLE,
    SAFE,
    UNSAFE,
    assess_reconciliation,
    failure_action,
    runtime_health,
)

SHA = "a" * 40


class V7RuntimeChaosContractTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def _base(self, root: Path) -> None:
        self._write(
            root / "control/runtime_status.json",
            {
                "version": 7,
                "timestamp": 1000,
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "model_sha": SHA,
                "pid": 99999999,
                "killed": False,
            },
        )
        self._write(
            root / "control/portfolio_state.json",
            {
                "paper_only": True,
                "authenticated_execution": False,
                "killed": False,
                "drawdown": 0.01,
                "max_drawdown": 0.15,
                "sleeves": {},
            },
        )
        self._write(
            root / "ledger/execution.jsonl",
            {
                "event_type": "OPPORTUNITY",
                "strategy": "MICRO_MAKER_PRO",
                "model_sha": SHA,
                "paper_only": True,
                "authenticated_execution": False,
            },
        )

    def test_clean_dead_previous_runtime_is_safe_to_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._base(root)
            result = assess_reconciliation(root, SHA, now=1001)
            self.assertEqual(result.classification, SAFE)
            self.assertTrue(result.may_start)

    def test_duplicate_writer_never_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._base(root)
            lock = root / "control/runtime.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
            result = assess_reconciliation(root, SHA, now=1001)
            self.assertEqual(result.classification, UNSAFE)
            self.assertIn("duplicate_writer_live", result.reasons)

    def test_process_crash_is_bounded_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._base(root)
            runtime = json.loads((root / "control/runtime_status.json").read_text())
            runtime["killed"] = True
            self._write(root / "control/runtime_status.json", runtime)
            self._write(
                root / "control/KILL",
                {
                    "schema": "polymarket_v7_runtime_failure_v1",
                    "paper_only": True,
                    "authenticated_execution": False,
                    "model_sha": SHA,
                    "dead_pid": 99999999,
                },
            )
            result = assess_reconciliation(root, SHA, now=1001)
            self.assertEqual(result.classification, RECOVERABLE)
            self.assertIn("recoverable_process_crash", result.reasons)

    def test_restart_rebuilds_safe_paper_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._base(root)
            portfolio = json.loads((root / "control/portfolio_state.json").read_text())
            portfolio["sleeves"] = {"micro_maker": {"inventory": 7.0}}
            self._write(root / "control/portfolio_state.json", portfolio)
            result = assess_reconciliation(root, SHA, now=1001)
            self.assertEqual(result.classification, RECOVERABLE)
            self.assertTrue(result.paper_inventory_present)
            self.assertIn("paper_inventory_rebuild_required", result.reasons)

    def test_corrupt_or_unsafe_ledger_never_auto_restarts(self) -> None:
        for payload, reason in (
            ("not-json\n", "ledger_invalid_json:1"),
            (json.dumps({"model_sha": SHA, "paper_only": False, "authenticated_execution": False}) + "\n", "ledger_unsafe_record:1"),
            (json.dumps({"model_sha": "b" * 40, "paper_only": True, "authenticated_execution": False}) + "\n", "ledger_sha_mismatch:1"),
            (json.dumps({"model_sha": SHA, "paper_only": True, "authenticated_execution": False}), "ledger_incomplete_tail"),
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._base(root)
                (root / "ledger/execution.jsonl").write_text(payload, encoding="utf-8")
                result = assess_reconciliation(root, SHA, now=1001)
                self.assertEqual(result.classification, UNSAFE)
                self.assertIn(reason, result.reasons)

    def test_clock_jump_and_kill_switch_are_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._base(root)
            runtime = json.loads((root / "control/runtime_status.json").read_text())
            runtime["timestamp"] = 2000
            self._write(root / "control/runtime_status.json", runtime)
            result = assess_reconciliation(root, SHA, now=1000)
            self.assertEqual(result.classification, UNSAFE)
            self.assertIn("runtime_clock_in_future", result.reasons)

    def test_bilateral_flow_maker_selector_is_operational(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "control/runtime_status.json",
                {
                    "version": 7,
                    "timestamp": 1000,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "model_sha": SHA,
                    "pid": os.getpid(),
                    "killed": False,
                    "state": "running",
                },
            )
            self._write(
                root / "micro_maker/selector_status.json",
                {
                    "timestamp_ms": 1_000_000,
                    "model_sha": SHA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "ready": True,
                    "state": "OPERATIONAL_BILATERAL_FLOW",
                },
            )
            self._write(
                root / "micro_maker/status.json",
                {
                    "timestamp_ms": 1_000_000,
                    "model_sha": SHA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "killed": False,
                    "source": "full_visible_bid_depth_net_verified_fee_and_slippage",
                },
            )
            self._write(
                root / "micro_maker/rotation_status.json",
                {
                    "schema": "polymarket_v7_maker_cohort_rotation_status_v1",
                    "timestamp_ms": 1_000_000,
                    "model_sha": SHA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "state": "RUNNING",
                },
            )
            result = runtime_health(root, SHA, now=1000, stale_seconds=30)
            self.assertEqual(result.classification, SAFE)
            self.assertNotIn("maker_selector_not_ready", result.reasons)

    def test_every_emitted_operational_rotation_state_is_runtime_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "control/runtime_status.json",
                {
                    "version": 7,
                    "timestamp": 1000,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "model_sha": SHA,
                    "pid": os.getpid(),
                    "killed": False,
                    "state": "running",
                },
            )
            self._write(
                root / "micro_maker/selector_status.json",
                {
                    "timestamp_ms": 1_000_000,
                    "model_sha": SHA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "real_order_submission": False,
                    "ready": True,
                    "state": "OPERATIONAL_BILATERAL_FLOW",
                },
            )
            self._write(
                root / "micro_maker/status.json",
                {
                    "timestamp_ms": 1_000_000,
                    "model_sha": SHA,
                    "paper_only": True,
                    "authenticated_execution": False,
                    "killed": False,
                    "source": "full_visible_bid_depth_net_verified_fee_and_slippage",
                },
            )
            rotation_path = root / "micro_maker/rotation_status.json"
            for state in sorted(MAKER_ROTATION_OPERATIONAL_STATES):
                with self.subTest(state=state):
                    self._write(
                        rotation_path,
                        {
                            "timestamp_ms": 1_000_000,
                            "model_sha": SHA,
                            "paper_only": True,
                            "authenticated_execution": False,
                            "real_order_submission": False,
                            "state": state,
                        },
                    )
                    result = runtime_health(root, SHA, now=1000, stale_seconds=30)
                    self.assertEqual(result.classification, SAFE)
                    self.assertNotIn("maker_cohort_not_ready", result.reasons)

    def test_failure_isolation_matrix_covers_deterministic_chaos_scenarios(self) -> None:
        policy = json.loads((ROOT / "config/v7_runtime_supervision.json").read_text())
        expected = {
            "signer_rate_limit": "quarantine_signer_and_reconcile",
            "order_heartbeat_expired": "cancel_all_and_reconcile",
            "matching_engine_restart": "cancel_and_backoff_until_post_only_recovery",
            "matching_engine_cancel_only": "cancel_only_until_reconciled",
            "market_ws": "withdraw_and_restart",
            "sequence_gap": "invalidate_book_and_resnapshot",
            "duplicate_update": "deduplicate",
            "out_of_order_update": "reject_and_resnapshot",
            "rest_stale": "quarantine",
            "external_feed": "quarantine",
            "oracle_unknown": "quarantine",
            "dns": "isolate_and_retry",
            "http_timeout": "isolate_and_retry",
            "ledger_write": "withdraw_and_quarantine",
            "disk_pressure_critical": "withdraw_and_quarantine",
            "monitoring": "restart_monitoring_only",
            "clock_jump": "withdraw_and_quarantine",
            "cancel_pending_reconnect": "hold_withdrawn_until_reconciled",
        }
        for failure, action in expected.items():
            with self.subTest(failure=failure):
                self.assertEqual(failure_action(policy, failure)["action"], action)
        self.assertTrue(failure_action(policy, "ledger_write")["critical"])
        self.assertFalse(failure_action(policy, "external_feed")["critical"])

        recovery = policy["clob_v2_recovery"]
        self.assertEqual(recovery["http_425_backoff_initial_seconds"], 1)
        self.assertEqual(recovery["http_425_backoff_max_seconds"], 30)
        self.assertEqual(recovery["post_restart_post_only_seconds"], 120)
        self.assertTrue(recovery["cancel_only_allows_cancel"])
        self.assertTrue(recovery["private_reconciliation_required_after_stream_disconnect"])
        self.assertTrue(recovery["paper_only"])
        self.assertFalse(recovery["authenticated_execution"])
        self.assertFalse(recovery["real_order_submission"])

        heartbeat = policy["order_heartbeat"]
        self.assertEqual(heartbeat["interval_seconds"], 5)
        self.assertEqual(heartbeat["maximum_ack_age_seconds"], 10)
        self.assertEqual(heartbeat["missing_ack_action"], "cancel_all_and_reconcile")
        self.assertTrue(heartbeat["paper_only"])
        self.assertFalse(heartbeat["authenticated_execution"])
        self.assertFalse(heartbeat["real_order_submission"])

        rate_limit = policy["signer_rate_limit"]
        self.assertEqual(rate_limit["rolling_window_seconds"], 1)
        self.assertEqual(rate_limit["maximum_regular_requests"], 10)
        self.assertEqual(rate_limit["maximum_emergency_requests"], 10)
        self.assertEqual(rate_limit["maximum_total_requests"], 20)
        self.assertEqual(rate_limit["reserve_emergency_for"], ["cancel", "heartbeat"])
        self.assertEqual(rate_limit["clock_regression_action"], "quarantine_signer_and_reconcile")
        self.assertTrue(rate_limit["paper_only"])
        self.assertFalse(rate_limit["authenticated_execution"])
        self.assertFalse(rate_limit["real_order_submission"])


if __name__ == "__main__":
    unittest.main()
