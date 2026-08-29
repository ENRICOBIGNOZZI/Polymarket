from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v6_external_bridge", ROOT / "scripts" / "v6_external_bridge.py")
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class V6ExternalFeedStartupTest(unittest.TestCase):
    def test_external_feed_is_materialized_before_external_engine_starts(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn("refresh_external_feed(){", loop)
        self.assertIn("market_key,q_yes,confidence,source,timestamp", loop)
        startup = loop.index("wait_for_owned_proxy ||")
        refresh = loop.index("refresh_external_feed\n", startup)
        external_start = loop.index("start_recorder;start_broker;start_external;write_supervisor", startup)
        self.assertLess(refresh, external_start)
        self.assertIn('last_external="$(date +%s)"', loop)

    def test_periodic_refresh_uses_same_fail_closed_materializer(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        periodic = loop.index("if ((now-last_external>=60));then")
        tail = loop[periodic: periodic + 220]
        self.assertIn("refresh_external_feed", tail)
        self.assertNotIn("v6_external_bridge.py --output", tail)

    def test_candidate_local_gate_is_not_enough_for_champion_feed(self) -> None:
        report = {
            "alpha_factory_evidence": {"candidate_id": "direct-1", "integration_evidence_pass": False},
            "backtest": {
                "candidates": [
                    {
                        "candidate_id": "direct-1",
                        "source": "approved_model",
                        "feature_name": "external_probability",
                        "gate_pass": True,
                    }
                ]
            },
        }
        self.assertEqual(bridge.approved_direct_models(report), set())

    def test_only_exact_integration_approved_direct_probability_is_authorized(self) -> None:
        report = {
            "alpha_factory_evidence": {"candidate_id": "direct-1", "integration_evidence_pass": True},
            "backtest": {
                "candidates": [
                    {
                        "candidate_id": "direct-1",
                        "source": "approved_model",
                        "feature_name": "external_probability",
                        "gate_pass": True,
                    },
                    {
                        "candidate_id": "raw-1",
                        "source": "binance",
                        "feature_name": "return_1h",
                        "gate_pass": True,
                    },
                ]
            },
        }
        self.assertEqual(
            bridge.approved_direct_models(report),
            {("approved_model", "external_probability")},
        )

    def test_bridge_hard_abort_revokes_preexisting_signal_before_remote_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "external_signals.csv"
            status = root / "external_bridge_status.json"
            output.write_text(
                "market_key,q_yes,confidence,source,timestamp\n"
                "stale-market,0.9,0.9,old:external_probability,1\n",
                encoding="utf-8",
            )
            status.write_text('{"materialized_signals": 1}\n', encoding="utf-8")
            argv = [
                "v6_external_bridge.py",
                "--output",
                str(output),
                "--status",
                str(status),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                bridge, "fetch_text", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    bridge.main()

            self.assertEqual(output.read_text(encoding="utf-8"), bridge.EMPTY_FEED)
            payload = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(payload["materialized_signals"], 0)
            self.assertEqual(payload["failures"], ["bridge_incomplete"])
            self.assertFalse(payload["integration_evidence_pass"])


if __name__ == "__main__":
    unittest.main()
