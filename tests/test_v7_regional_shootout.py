from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_regional_shootout", ROOT / "scripts/v7_regional_shootout.py")
assert SPEC and SPEC.loader
shootout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shootout)
POLICY = json.loads((ROOT / "config/v7_latency_slo.json").read_text())["regional_shootout"]
SHA = "a" * 40


def probe(region: str, *, p999: int, failed: int = 0, sha: str = SHA) -> dict:
    samples = 1000
    return {"schema": shootout.SCHEMA, "endpoint": POLICY["public_probe_endpoint"], "region": region, "exact_code_sha": sha,
            "started_wall_ms": 1_000, "finished_wall_ms": 1_000 + 86_400_000, "samples": samples, "warmup": 3,
            "successful_samples": samples - failed, "failed_samples": failed, "warmup_failed_samples": 0, "primary_ip": "192.0.2.1",
            "connection_reused_samples": samples - failed - 1, "new_connections": 1, "reconnect_count": 0,
            "timings_ns": {stage: {"p50": 1, "p90": 2, "p95": 3, "p99": 4, "p99_9": p999, "max": p999 + 1} for stage in shootout.TIMINGS},
            "paper_only": True, "authenticated_execution": False, "real_order_submission": False, "measures_order_or_cancel_ack": False}


class RegionalShootoutTests(unittest.TestCase):
    def test_only_complete_healthy_same_sha_set_selects_lowest_tail_region(self) -> None:
        policy = json.loads((ROOT / "config/v7_latency_slo.json").read_text())
        probes = [probe(region, p999=index + 10) for index, region in enumerate(POLICY["candidate_regions"])]
        result = shootout.assess(policy, probes)
        self.assertEqual(result["state"], "REGIONAL_SHOOTOUT_READY_FOR_MANUAL_SELECTION")
        self.assertEqual(result["selected_region"], POLICY["candidate_regions"][0])
        self.assertFalse(result["live_execution_authorized"])
        self.assertFalse(result["network_or_clob_proven"])

    def test_missing_region_bad_health_or_mixed_sha_keeps_evidence_state(self) -> None:
        policy = json.loads((ROOT / "config/v7_latency_slo.json").read_text())
        partial = [probe(POLICY["candidate_regions"][0], p999=10)]
        result = shootout.assess(policy, partial)
        self.assertIsNone(result["selected_region"])
        self.assertIn("candidate_regions_missing", result["reason_codes"])
        probes = [probe(region, p999=index + 10, sha=("b" * 40 if index == 1 else SHA)) for index, region in enumerate(POLICY["candidate_regions"])]
        result = shootout.assess(policy, probes)
        self.assertIn("exact_code_sha_mismatch", result["reason_codes"])
        probes = [probe(region, p999=index + 10, failed=(20 if index == 0 else 0)) for index, region in enumerate(POLICY["candidate_regions"])]
        result = shootout.assess(policy, probes)
        self.assertFalse(next(row for row in result["regional_results"] if row["region"] == POLICY["candidate_regions"][0])["eligible"])


if __name__ == "__main__":
    unittest.main()
