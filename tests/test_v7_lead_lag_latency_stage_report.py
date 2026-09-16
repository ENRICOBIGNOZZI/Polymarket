from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent
from v7_lead_lag_latency_stage_report import stage_summary, summarize

SHA = "a" * 40


def fill(index: int = 0, *, negative_candidate: bool = False) -> LedgerEvent:
    trigger = 1_800_000_000_000_000_000 + index * 1_000_000_000
    candidate = trigger + (20_000_000 if not negative_candidate else 60_000_000)
    coordinator = trigger + 40_000_000
    decision_ms = (trigger + 50_000_000) // 1_000_000
    receipt = {
        "decision_timestamp_ns": coordinator,
        "selected_replay_key": f"replay-{index}",
    }
    return LedgerEvent(
        event_type="FILL", strategy="CRYPTO_SETTLEMENT_ENGINE", model_sha=SHA,
        record_id=f"record-{index}", recorded_ts_ms=decision_ms + 1,
        opportunity_id=f"replay-{index}", order_id=f"order-{index}", fill_id=f"fill-{index}",
        position_id=f"position-{index}", market_id=f"market-{index}", event_id=f"event-{index}",
        token_id=f"token-{index}", decision_ts_ms=decision_ms,
        exchange_ts_ms=decision_ms - 2, receive_ts_ms=decision_ms - 1,
        book_snapshot_id=f"book-{index}", side="BUY", fill_price=.5, filled_size=5., complete=True,
        fee=.01, fee_source="SYNTHETIC", metadata={
            "model_family": "lead_lag_taker_v1", "signal_trigger_wall_ns": trigger,
            "signal_age_ms_at_fill": 50.0, "coordinator_receipt": receipt,
        },
    )


def runtime_events(path: Path, count: int = 1, *, negative_candidate: bool = False) -> None:
    rows = []
    for index in range(count):
        trigger = 1_800_000_000_000_000_000 + index * 1_000_000_000
        rows.append({"event": "CANDIDATE", "timestamp_ns": trigger + (20_000_000 if not negative_candidate else 60_000_000),
                     "replay_key": f"replay-{index}", "model_family": "lead_lag_taker_v1"})
    rows.append({"event": "REJECTED", "timestamp_ns": 1_800_000_000_999_000_000,
                 "reason": "ARRIVAL_NO_CHASE_OR_DEPTH", "model_family": "lead_lag_taker_v1"})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


class LeadLagLatencyStageReportTest(unittest.TestCase):
    def test_observed_wall_stages_are_separate_from_synthetic_recording_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"; runtime_events(events)
            report = summarize([fill()], runtime_events=events)
        self.assertEqual(report["fill_count"], 1)
        self.assertEqual(report["stages_ns"]["signal_to_candidate_wall_ns"]["p50"], 20_000_000)
        self.assertEqual(report["stages_ns"]["candidate_to_coordinator_decision_wall_ns"]["p50"], 20_000_000)
        self.assertEqual(report["stages_ns"]["coordinator_to_paper_arrival_decision_wall_ns"]["p50"], 10_000_000)
        self.assertEqual(report["synthetic_ledger_recording_offset_ns"]["p50"], 1_000_000)
        self.assertTrue(report["synthetic_recording_offset_excluded_from_latency_claims"])
        self.assertFalse(report["exchange_ack_available"])
        self.assertFalse(report["real_fill_time_available"])
        self.assertFalse(report["clock_domain_proven"])

    def test_small_sample_never_claims_p99_or_p999(self) -> None:
        result = stage_summary(list(range(50)))
        self.assertIsNotNone(result["p95"])
        self.assertIsNone(result["p99"])
        self.assertIsNone(result["p99_9"])
        result = stage_summary(list(range(1000)))
        self.assertIsNotNone(result["p99"])
        self.assertIsNotNone(result["p99_9"])

    def test_negative_stage_is_flagged_and_not_added_to_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"; runtime_events(events, negative_candidate=True)
            report = summarize([fill(negative_candidate=True)], runtime_events=events)
        self.assertEqual(report["inconsistency_counts"]["candidate_to_coordinator_decision_wall_ns:NEGATIVE"], 1)
        self.assertEqual(report["stages_ns"]["candidate_to_coordinator_decision_wall_ns"]["samples"], 0)

    def test_rejections_remain_in_opportunity_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"; runtime_events(events)
            report = summarize([fill()], runtime_events=events)
        self.assertEqual(report["runtime_rejection_reason_counts"], {"ARRIVAL_NO_CHASE_OR_DEPTH": 1})
        self.assertIn("fill-only latency is not the opportunity denominator", report["coordinated_omission_note"])


if __name__ == "__main__":
    unittest.main()
