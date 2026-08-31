from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHA = "c" * 40


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scorecard = load("v7_real_pnl_scorecard")


def verified_report(total: int) -> dict:
    unsigned = {
        "state": "REAL_PNL_RECONCILED_UNSIGNED", "real_pnl_verified": False, "model_sha": SHA,
        "journal_head_hash": "c" * 64, "reconstructed_realized_pnl_units": total,
    }
    report_sha = scorecard.digest(unsigned)
    attestation = {
        "schema": scorecard.ATTESTATION_SCHEMA, "operator_id": "test-operator",
        "report_sha256": report_sha, "model_sha": SHA, "journal_head_hash": unsigned["journal_head_hash"],
        "algorithm": "HMAC-SHA256", "signature": "b" * 64,
    }
    return {**unsigned, "state": "REAL_PNL_VERIFIED", "real_pnl_verified": True,
            "report_sha256": report_sha, "attestation": attestation}


def write_samples(path: Path, *, report_sha: str, count: int = 30, pnl: int = 100) -> None:
    tip = scorecard.GENESIS_HASH
    rows = []
    for index in range(count):
        raw = {
            "model_sha": SHA, "report_sha256": report_sha, "sample_id": f"sample-{index}",
            "event_cluster": f"event-{index}", "regime": f"regime-{index % 3}",
            "terminal_ts_ms": index + 1, "gross_pnl_units": pnl + 2, "fee_units": 1,
            "slippage_units": 1, "reward_units": 0, "capital_units": 10_000,
            "capacity_tier": index % 3 + 1, "record_hash": None,
        }
        row = scorecard.seal_sample(raw, tip)
        tip = row["record_hash"]
        rows.append(row)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


class RealPnlScorecardTests(unittest.TestCase):
    def test_real_verified_clustered_cost_stressed_evidence_can_reach_manual_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "samples.jsonl"
            report = verified_report(3_000)
            write_samples(samples, report_sha=report["report_sha256"])
            result = scorecard.scorecard(report, samples)
            self.assertEqual(result["state"], "REAL_PNL_ECONOMIC_PROOF")
            self.assertFalse(result["automatic_promotion"])
            self.assertFalse(result["world_class_candidate"])
            self.assertGreater(result["cost_stress"]["2.0x"]["cluster_equal_weighted"]["lower"], 0)
            self.assertGreater(result["cost_stress"]["2.0x"]["reward_free_cluster_equal_weighted"]["lower"], 0)

    def test_paper_or_unreconciled_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "samples.jsonl"
            report = verified_report(3_000)
            write_samples(samples, report_sha=report["report_sha256"])
            report["state"] = "REAL_PNL_RECONCILED_UNSIGNED"
            with self.assertRaisesRegex(scorecard.ScorecardError, "real_pnl_verified_required"):
                scorecard.scorecard(report, samples)

    def test_missing_evidence_and_tampering_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "samples.jsonl"
            report = verified_report(200)
            write_samples(samples, report_sha=report["report_sha256"], count=2)
            result = scorecard.scorecard(report, samples)
            self.assertEqual(result["state"], "MORE_EVIDENCE_REQUIRED")
            self.assertIn("insufficient_event_clusters", result["reason_codes"])
            lines = samples.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[1]); row["gross_pnl_units"] = 999
            lines[1] = json.dumps(row, sort_keys=True)
            samples.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(scorecard.ScorecardError, "record_hash_mismatch"):
                scorecard.scorecard(report, samples)

    def test_spliced_report_or_attestation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = verified_report(3_000)
            samples = Path(directory) / "samples.jsonl"
            write_samples(samples, report_sha=report["report_sha256"])
            report["reconstructed_realized_pnl_units"] = 3_001
            with self.assertRaisesRegex(scorecard.ScorecardError, "report_hash_mismatch"):
                scorecard.scorecard(report, samples)


if __name__ == "__main__":
    unittest.main()
