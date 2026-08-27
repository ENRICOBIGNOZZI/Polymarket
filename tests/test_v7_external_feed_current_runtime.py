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
        signals = json.dumps({
            "market_id": "m1",
            "observed_ts": now - 5,
            "source": "kalshi",
            "feature_name": "external_probability",
            "q_external": 0.61,
            "confidence": 0.9,
        }) + "\n"
        output, status = bridge.materialize(self.report(generated_ts=now), signals, now=now, max_age_seconds=7200, min_confidence=0.35)
        lines = output.strip().splitlines()
        self.assertEqual(lines[0], "market_key,q_yes,confidence,source,timestamp")
        self.assertEqual(len(lines), 2)
        self.assertIn("candidate=direct-A", lines[1])
        self.assertNotIn("direct-B", lines[1])
        self.assertEqual(status["approved_candidate_id"], "direct-A")
        self.assertEqual(status["materialized_signals"], 1)

    def test_stale_report_is_header_only(self) -> None:
        now = int(time.time())
        signals = json.dumps({
            "market_id": "m1",
            "observed_ts": now - 5,
            "source": "kalshi",
            "feature_name": "external_probability",
            "q_external": 0.61,
            "confidence": 0.9,
        }) + "\n"
        output, status = bridge.materialize(self.report(generated_ts=now - 7201), signals, now=now, max_age_seconds=7200, min_confidence=0.35)
        self.assertEqual(output, bridge.EMPTY_FEED)
        self.assertEqual(status["materialized_signals"], 0)
        self.assertIn("report_stale_or_invalid_timestamp", status["failures"])

    def test_local_gate_alone_is_not_authorization(self) -> None:
        self.assertIsNone(bridge.approved_direct_candidate(self.report(approved=False)))

    def test_entrypoint_runs_bridge_before_loop(self) -> None:
        text = (ROOT / "scripts" / "run_paper.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("python3 scripts/v7_external_bridge.py"), text.index('exec bash "$LOOP" "$CONFIG" "$RUN_ROOT"'))
        self.assertNotIn("--allow-unvalidated", text)


if __name__ == "__main__":
    unittest.main()
