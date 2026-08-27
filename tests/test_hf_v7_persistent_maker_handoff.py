#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hf_v7_persistent_maker_handoff.py"
spec = importlib.util.spec_from_file_location("hf_v7_persistent_maker_handoff", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class PersistentMakerHandoffTests(unittest.TestCase):
    def _write_status(self, path: Path, **overrides) -> None:
        status = {
            "schema_version": 1,
            "timestamp_ms": 40_000,
            "mode": "shadow",
            "real_order_submission": False,
            "tokens": 4,
            "freshness_ready_tokens": 4,
            "ws_errors": 0,
            "current_stale_opportunities": 0,
        }
        status.update(overrides)
        path.write_text(json.dumps(status))

    def _write_directives(self, path: Path) -> None:
        path.write_text(json.dumps({
            "paper_v7_authorization": {
                "paper_only": True,
                "authenticated_execution": False,
                "min_net_edge": 0.00005,
            }
        }))

    def _write_csv(self, path: Path, *, bad_reject: bool = False, bad_legs: bool = False) -> None:
        fields = [
            "observed_ts_ms", "kind", "id", "event_id", "hard_arbitrage", "executable",
            "reject_reason", "raw_edge_per_share", "net_edge_per_share", "feed_latency_ms", "legs",
        ]
        rows = []
        good_legs = "111:t_yes:YES_POST_ONLY:YES:0.45:1:x|111:t_no:NO_POST_ONLY:NO:0.53:1:x"
        malformed = "111:t_yes:YES_POST_ONLY:YES:0.45:1:x"
        for i, ts in enumerate((1_000, 12_000, 25_000)):
            rows.append({
                "observed_ts_ms": ts,
                "kind": "MAKER_COMPLETE_SET_SHADOW",
                "id": "maker-binary:111",
                "event_id": "evt-1",
                "hard_arbitrage": "0",
                "executable": "1",
                "reject_reason": "blocked" if bad_reject and i == 0 else "",
                "raw_edge_per_share": "0.012",
                "net_edge_per_share": "0.007",
                "feed_latency_ms": str(50 + i),
                "legs": malformed if bad_legs and i == 0 else good_legs,
            })
        for ts in (30_000, 31_000, 32_000):
            rows.append({
                "observed_ts_ms": ts,
                "kind": "MAKER_COMPLETE_SET_SHADOW",
                "id": "maker-binary:222",
                "event_id": "evt-2",
                "hard_arbitrage": "0",
                "executable": "1",
                "reject_reason": "",
                "raw_edge_per_share": "0.020",
                "net_edge_per_share": "0.015",
                "feed_latency_ms": "40",
                "legs": "222:a:YES_POST_ONLY:YES:0.30:1:x|222:b:NO_POST_ONLY:NO:0.68:1:x",
            })
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_healthy_window_pre_registers_only_persistent_stressed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            csv_path = tmp / "opps.csv"
            status_path = tmp / "status.json"
            directives_path = tmp / "directives.json"
            self._write_csv(csv_path)
            self._write_status(status_path)
            self._write_directives(directives_path)

            status = json.loads(status_path.read_text())
            module.validate_status(status)
            min_edge = module.load_authorized_min_edge(directives_path)
            observations, max_ts = module.load_observations(csv_path)
            report = module.build_handoff(
                observations,
                authorized_min_edge=min_edge,
                stress_bps=10.0,
                min_observations=3,
                min_span_ms=20_000,
                source_head_sha="a" * 40,
                source_run_id="run",
                source_artifact_id="artifact",
                source_window_end_ms=max(max_ts, int(status["timestamp_ms"])),
            )
            self.assertEqual(report["candidate_count"], 1)
            candidate = report["candidates"][0]
            self.assertEqual(candidate["market_id"], "111")
            self.assertAlmostEqual(candidate["stressed_net_edge_min"], 0.006)
            self.assertFalse(report["promotion_allowed"])
            self.assertFalse(report["same_window_fill_or_pnl_credit"])
            self.assertGreater(report["prospective_not_before_ms"], status["timestamp_ms"])

    def test_status_fails_closed_when_strict_freshness_is_incomplete(self) -> None:
        status = {
            "mode": "shadow",
            "real_order_submission": False,
            "tokens": 4,
            "freshness_ready_tokens": 3,
            "ws_errors": 0,
            "current_stale_opportunities": 0,
        }
        with self.assertRaisesRegex(module.EvidenceError, "incomplete_strict_freshness_coverage"):
            module.validate_status(status)

    def test_executable_row_with_reject_reason_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opps.csv"
            self._write_csv(path, bad_reject=True)
            with self.assertRaisesRegex(module.EvidenceError, "executable_row_has_reject_reason"):
                module.load_observations(path)

    def test_malformed_complete_set_leg_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opps.csv"
            self._write_csv(path, bad_legs=True)
            with self.assertRaisesRegex(module.EvidenceError, "maker_complete_set_requires_two_legs"):
                module.load_observations(path)

    def test_authority_requires_paper_only_and_auth_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "directives.json"
            path.write_text(json.dumps({
                "paper_v7_authorization": {
                    "paper_only": True,
                    "authenticated_execution": True,
                    "min_net_edge": 0.00005,
                }
            }))
            with self.assertRaisesRegex(module.EvidenceError, "unsafe_operator_directive"):
                module.load_authorized_min_edge(path)

    def test_frozen_recurrence_evidence_is_prospective_and_not_execution_credit(self) -> None:
        path = ROOT / "research" / "hf_v7_persistent_maker_recurrence_window2_20260827.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        cutoff = int(report["preregistered_source"]["prospective_not_before_ms"])
        recurrence = report["prospective_recurrence"]["candidate"]
        first = int(recurrence["first_later_observed_ts_ms"])

        self.assertGreaterEqual(first, cutoff)
        self.assertEqual(int(recurrence["delay_after_preregistered_cutoff_ms"]), first - cutoff)
        self.assertEqual(recurrence["opportunity_id"], "maker-binary:1321564")
        self.assertGreater(float(recurrence["net_edge_per_share"]), 0.0)
        self.assertGreater(float(recurrence["stressed_net_edge_per_share"]), 0.0)
        self.assertEqual(int(recurrence["exchange_ts_ms"]), 0)
        self.assertFalse(report["later_window"]["authority_valid_execution_window"])
        self.assertFalse(report["paper_fill_claim"])
        self.assertFalse(report["realized_pnl_claim"])
        self.assertFalse(recurrence["paper_fill"])
        self.assertFalse(recurrence["paired_completion"])
        self.assertIsNone(recurrence["realized_fill_conditioned_pnl_usd"])
        self.assertFalse(report["promotion_allowed"])

    def test_second_recurrence_strengthens_identity_but_not_execution_or_stress_credit(self) -> None:
        path = ROOT / "research" / "hf_v7_persistent_maker_recurrence_window3_20260827.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        cutoff = int(report["preregistered_source"]["prospective_not_before_ms"])
        prior_final = int(report["prior_prospective_recurrence"]["final_observed_ts_ms"])
        recurrence = report["prospective_recurrence"]
        first = int(recurrence["first_later_observed_ts_ms"])
        final = int(recurrence["final_later_observed_ts_ms"])

        self.assertGreater(first, cutoff)
        self.assertGreater(first, prior_final)
        self.assertGreater(final, first)
        self.assertEqual(int(recurrence["delay_after_preregistered_cutoff_ms"]), first - cutoff)
        self.assertEqual(int(recurrence["gap_after_prior_recurrence_ms"]), first - prior_final)
        self.assertEqual(recurrence["candidate_id"], "maker-binary:1321564")
        self.assertTrue(recurrence["second_independent_post_registration_recurrence"])
        self.assertTrue(recurrence["all_rows_above_authorized_0_5bp_floor"])
        self.assertFalse(recurrence["passes_original_all_rows_extra_10bp_frontier"])
        self.assertAlmostEqual(float(recurrence["minimum_edge_after_extra_10bp_stress"]), 0.0)
        self.assertEqual(int(recurrence["first_row_exchange_ts_ms"]), 0)
        self.assertEqual(int(recurrence["final_row_exchange_ts_ms"]), 0)
        self.assertFalse(report["later_window"]["authority_valid_execution_window"])
        self.assertFalse(report["paper_fill_claim"])
        self.assertFalse(report["realized_pnl_claim"])
        self.assertFalse(recurrence["paper_fill"])
        self.assertFalse(recurrence["paired_completion"])
        self.assertIsNone(recurrence["realized_fill_conditioned_pnl_usd"])
        self.assertFalse(report["promotion_allowed"])


if __name__ == "__main__":
    unittest.main()
