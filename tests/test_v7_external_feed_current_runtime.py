from __future__ import annotations

import importlib.util
import json
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_external_bridge", ROOT / "scripts" / "v7_external_bridge.py")
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class V7ExternalFeedCurrentRuntimeTest(unittest.TestCase):
    def report(self, approved: bool = True, generated_ts: int | None = None) -> dict:
        generated_ts = int(time.time()) if generated_ts is None else generated_ts
        return {
            "generated_ts": generated_ts,
            "alpha_factory_evidence": {
                "candidate_id": "direct-A",
                "integration_evidence_pass": approved,
            },
            "backtest": {
                "candidates": [
                    {"candidate_id": "direct-A", "source": "kalshi", "feature_name": "external_probability", "horizon_seconds": 86400, "gate_pass": True},
                    {"candidate_id": "direct-B", "source": "kalshi", "feature_name": "external_probability", "horizon_seconds": 3600, "gate_pass": True},
                ]
            },
        }

    def signal(self, *, candidate_id: str = "direct-A", horizon_seconds: int = 86400, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        return json.dumps({
            "market_id": "m1",
            "observed_ts": now - 5,
            "source": "kalshi",
            "feature_name": "external_probability",
            "candidate_id": candidate_id,
            "horizon_seconds": horizon_seconds,
            "q_external": 0.61,
            "confidence": 0.9,
        }) + "\n"

    def test_exact_candidate_identity_is_preserved(self) -> None:
        candidate = bridge.approved_direct_candidate(self.report())
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["candidate_id"], "direct-A")
        provenance = bridge.candidate_provenance(candidate)
        self.assertIn("candidate=direct-A", provenance)
        self.assertIn("horizon=86400", provenance)
        self.assertNotIn("direct-B", provenance)

    def test_materializer_uses_exact_approved_identity(self) -> None:
        now = int(time.time())
        output, status = bridge.materialize(
            self.report(generated_ts=now),
            self.signal(now=now),
            now=now,
            max_age_seconds=7200,
            min_confidence=0.35,
        )
        lines = output.strip().splitlines()
        self.assertEqual(lines[0], "market_key,q_yes,confidence,source,timestamp")
        self.assertEqual(len(lines), 2)
        self.assertIn("candidate=direct-A", lines[1])
        self.assertNotIn("direct-B", lines[1])
        self.assertEqual(status["approved_candidate_id"], "direct-A")
        self.assertEqual(status["materialized_signals"], 1)

    def test_same_source_different_candidate_is_rejected(self) -> None:
        now = int(time.time())
        output, status = bridge.materialize(
            self.report(generated_ts=now),
            self.signal(candidate_id="direct-B", horizon_seconds=3600, now=now),
            now=now,
            max_age_seconds=7200,
            min_confidence=0.35,
        )
        self.assertEqual(output, bridge.EMPTY_FEED)
        self.assertEqual(status["materialized_signals"], 0)

    def test_missing_candidate_identity_is_rejected(self) -> None:
        now = int(time.time())
        row = json.loads(self.signal(now=now))
        row.pop("candidate_id")
        output, status = bridge.materialize(
            self.report(generated_ts=now),
            json.dumps(row) + "\n",
            now=now,
            max_age_seconds=7200,
            min_confidence=0.35,
        )
        self.assertEqual(output, bridge.EMPTY_FEED)
        self.assertEqual(status["materialized_signals"], 0)

    def test_stale_report_is_header_only(self) -> None:
        now = int(time.time())
        output, status = bridge.materialize(
            self.report(generated_ts=now - 7201),
            self.signal(now=now),
            now=now,
            max_age_seconds=7200,
            min_confidence=0.35,
        )
        self.assertEqual(output, bridge.EMPTY_FEED)
        self.assertEqual(status["materialized_signals"], 0)
        self.assertIn("report_stale_or_invalid_timestamp", status["failures"])

    def test_local_gate_alone_is_not_authorization(self) -> None:
        self.assertIsNone(bridge.approved_direct_candidate(self.report(approved=False)))

    def test_canonical_v7_loop_owns_external_bridge(self) -> None:
        text = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/v7_external_bridge.py", text)
        self.assertIn('while [[ ! -e "$KILL" ]]', text)
        self.assertNotIn("scripts/run_paper.sh", text)
        self.assertNotIn("--allow-unvalidated", text)


if __name__ == "__main__":
    unittest.main()
