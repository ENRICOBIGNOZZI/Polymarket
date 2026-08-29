from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_external_bridge as bridge


class V7ExternalBridgeProfitStackTest(unittest.TestCase):
    def report(self, approved: bool = True, generated_ts: int | None = None) -> dict:
        generated_ts = int(time.time()) if generated_ts is None else generated_ts
        return {
            "generated_ts": generated_ts,
            "alpha_factory_evidence": {"candidate_id": "direct-A", "integration_evidence_pass": approved},
            "backtest": {"candidates": [
                {"candidate_id": "direct-A", "source": "kalshi", "feature_name": "external_probability", "horizon_seconds": 86400, "gate_pass": True},
                {"candidate_id": "direct-B", "source": "kalshi", "feature_name": "external_probability", "horizon_seconds": 3600, "gate_pass": True},
            ]},
        }

    def signal(self, *, candidate_id: str = "direct-A", horizon_seconds: int = 86400, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        return json.dumps({
            "market_id": "m1", "observed_ts": now - 5, "source": "kalshi",
            "feature_name": "external_probability", "candidate_id": candidate_id,
            "horizon_seconds": horizon_seconds, "q_external": 0.61, "confidence": 0.9,
        }) + "\n"

    def test_exact_candidate_identity_and_horizon_are_required(self) -> None:
        now = int(time.time())
        output, status = bridge.materialize(self.report(generated_ts=now), self.signal(now=now), now=now, max_age_seconds=7200, min_confidence=0.35)
        self.assertEqual(len(output.strip().splitlines()), 2)
        self.assertIn("candidate=direct-A", output)
        self.assertIn("horizon=86400", output)
        self.assertEqual(status["materialized_signals"], 1)
        wrong, wrong_status = bridge.materialize(self.report(generated_ts=now), self.signal(candidate_id="direct-B", horizon_seconds=3600, now=now), now=now, max_age_seconds=7200, min_confidence=0.35)
        self.assertEqual(wrong, bridge.EMPTY_FEED)
        self.assertEqual(wrong_status["materialized_signals"], 0)

    def test_stale_or_unapproved_report_abstains(self) -> None:
        now = int(time.time())
        for report in (self.report(generated_ts=now - 7201), self.report(approved=False, generated_ts=now)):
            output, status = bridge.materialize(report, self.signal(now=now), now=now, max_age_seconds=7200, min_confidence=0.35)
            self.assertEqual(output, bridge.EMPTY_FEED)
            self.assertEqual(status["materialized_signals"], 0)

    def test_missing_candidate_identity_cannot_inherit_same_source_approval(self) -> None:
        now = int(time.time())
        row = json.loads(self.signal(now=now))
        row.pop("candidate_id")
        output, status = bridge.materialize(self.report(generated_ts=now), json.dumps(row) + "\n", now=now, max_age_seconds=7200, min_confidence=0.35)
        self.assertEqual(output, bridge.EMPTY_FEED)
        self.assertEqual(status["materialized_signals"], 0)

    def test_bridge_status_is_paper_only_and_authenticated_execution_disabled(self) -> None:
        now = int(time.time())
        _, status = bridge.materialize(self.report(generated_ts=now), self.signal(now=now), now=now, max_age_seconds=7200, min_confidence=0.35)
        self.assertTrue(status["paper_only"])
        self.assertFalse(status["authenticated_execution"])


if __name__ == "__main__":
    unittest.main()
