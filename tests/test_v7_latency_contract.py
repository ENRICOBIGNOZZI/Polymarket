#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class V7LatencyContractTests(unittest.TestCase):
    def test_persistent_curl_handle_and_keepalive(self) -> None:
        source = (ROOT / "src/http.cpp").read_text(encoding="utf-8")
        request = source.split("HttpResponse HttpClient::request", 1)[1]
        self.assertNotIn("curl_easy_init", request)
        self.assertNotIn("curl_easy_cleanup", request)
        self.assertIn("curl_easy_reset", request)
        self.assertIn("CURLOPT_TCP_KEEPALIVE", request)
        self.assertIn("CURLOPT_FRESH_CONNECT, 0L", request)
        self.assertIn("CURLOPT_FORBID_REUSE, 0L", request)

    def test_slo_is_paper_only_and_claim_safe(self) -> None:
        policy = json.loads((ROOT / "config/v7_latency_slo.json").read_text())
        self.assertEqual(policy["priority"], "P0")
        self.assertTrue(policy["safety"]["paper_only"])
        self.assertFalse(policy["safety"]["authenticated_execution"])
        self.assertFalse(policy["safety"]["real_order_submission"])
        self.assertFalse(policy["claim_boundaries"]["top_percentile_claim_allowed"])
        self.assertLessEqual(policy["ci_internal_guardrails_ns"]["receive_to_intent"]["p99"], 1_000_000)

    def test_all_required_stages_are_in_runtime_telemetry(self) -> None:
        source = (ROOT / "src/v7_market_maker_runtime.cpp").read_text(encoding="utf-8")
        for stage in ("parse_ns", "book_ns", "feature_ns", "decision_ns", "risk_ns",
                      "tx_queue_ns", "execution_ns", "receive_to_intent_ns"):
            self.assertIn(stage, source)

    def test_public_probe_cannot_send_orders(self) -> None:
        source = (ROOT / "src/v7_latency_probe.cpp").read_text(encoding="utf-8")
        self.assertIn("client.get", source)
        self.assertNotIn("post_json", source)
        self.assertIn("real_order_submission", source)

    def test_gate_accepts_only_bounded_non_venue_evidence(self) -> None:
        limits = json.loads((ROOT / "config/v7_latency_slo.json").read_text())[
            "ci_internal_guardrails_ns"
        ]
        stages = {
            stage: {percentile: ceiling for percentile, ceiling in percentiles.items()}
            for stage, percentiles in limits.items()
        }
        benchmark = {
            "paper_only": True,
            "representative_venue_replay": False,
            "includes_network_or_clob": False,
            "stages_ns": stages,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "benchmark.json"
            path.write_text(json.dumps(benchmark), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/v7_latency_gate.py"),
                 "--config", str(ROOT / "config/v7_latency_slo.json"),
                 "--benchmark", str(path)],
                check=False, capture_output=True, text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        verdict = json.loads(completed.stdout)
        self.assertTrue(verdict["passed"])
        self.assertFalse(verdict["network_or_clob_proven"])


if __name__ == "__main__":
    unittest.main()
