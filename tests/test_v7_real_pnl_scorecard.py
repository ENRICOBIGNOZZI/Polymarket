from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SHA = "c" * 40


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scorecard = load("v7_real_pnl_scorecard")
generator = load("v7_generate_pnl_attestation")


def key_pair(root: Path) -> tuple[Path, Path]:
    private_key, public_key = root / "attestation-private.pem", root / "attestation-public.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(private_key), "2048"], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["openssl", "rsa", "-in", str(private_key), "-pubout", "-out", str(public_key)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return private_key, public_key


def verified_report(total: int, *, private_key: Path, public_key: Path) -> dict:
    unsigned = {
        "state": "REAL_PNL_RECONCILED_UNSIGNED", "real_pnl_verified": False, "model_sha": SHA,
        "journal_head_hash": "c" * 64, "reconstructed_realized_pnl_units": total, "reason_codes": [],
    }
    report_sha = scorecard.digest(unsigned)
    attestation = generator.public_attestation(
        {**unsigned, "report_sha256": report_sha}, operator_id="test-operator",
        signing_key=private_key, public_key=public_key,
    )
    return {**unsigned, "state": "REAL_PNL_VERIFIED", "real_pnl_verified": True,
            "report_sha256": report_sha, "attestation": attestation}


def trust_registry(root: Path, report: dict) -> Path:
    path = root / "attestation-trust.json"
    path.write_text(json.dumps({
        "schema": "polymarket_v7_attestation_trust_registry_v1", "automatic_promotion": False,
        "trusted_attestors": [{"operator_id": report["attestation"]["operator_id"],
                                "public_key_sha256": report["attestation"]["public_key_sha256"],
                                "not_before": "2020-01-01T00:00:00Z", "not_after": "2030-01-01T00:00:00Z"}],
        "note": "test-only external trust root",
    }, sort_keys=True), encoding="utf-8")
    return path


def write_samples(path: Path, *, report_sha: str, count: int = 30, pnl: int = 100,
                  spacing_ms: int = 4 * 86_400_000, pnl_values: list[int] | None = None) -> None:
    if pnl_values is not None:
        count = len(pnl_values)
    tip = scorecard.GENESIS_HASH
    rows = []
    for index in range(count):
        sample_pnl = pnl_values[index] if pnl_values is not None else pnl
        raw = {
            "model_sha": SHA, "report_sha256": report_sha, "sample_id": f"sample-{index}",
            "event_cluster": f"event-{index}", "regime": f"regime-{index % 3}",
            "terminal_ts_ms": index * spacing_ms + 1, "gross_pnl_units": sample_pnl + 2, "fee_units": 1,
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
            private_key, public_key = key_pair(Path(directory))
            report = verified_report(3_000, private_key=private_key, public_key=public_key)
            trust = trust_registry(Path(directory), report)
            write_samples(samples, report_sha=report["report_sha256"])
            result = scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                         attestation_trust_registry=trust)
            self.assertEqual(result["state"], "REAL_PNL_ECONOMIC_PROOF")
            self.assertFalse(result["automatic_promotion"])
            self.assertFalse(result["world_class_candidate"])
            self.assertGreater(result["cost_stress"]["2.0x"]["cluster_equal_weighted"]["lower"], 0)
            self.assertGreater(result["cost_stress"]["2.0x"]["reward_free_cluster_equal_weighted"]["lower"], 0)
            with self.assertRaisesRegex(scorecard.ScorecardError, "untrusted_attestor"):
                scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                    attestation_trust_registry=ROOT / "config/v7_attestation_trust.json")

    def test_paper_or_unreconciled_report_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "samples.jsonl"
            private_key, public_key = key_pair(Path(directory))
            report = verified_report(3_000, private_key=private_key, public_key=public_key)
            trust = trust_registry(Path(directory), report)
            write_samples(samples, report_sha=report["report_sha256"])
            report["state"] = "REAL_PNL_RECONCILED_UNSIGNED"
            with self.assertRaisesRegex(scorecard.ScorecardError, "real_pnl_verified_required"):
                scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                    attestation_trust_registry=trust)

    def test_missing_evidence_and_tampering_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            samples = Path(directory) / "samples.jsonl"
            private_key, public_key = key_pair(Path(directory))
            report = verified_report(200, private_key=private_key, public_key=public_key)
            trust = trust_registry(Path(directory), report)
            write_samples(samples, report_sha=report["report_sha256"], count=2)
            result = scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                         attestation_trust_registry=trust)
            self.assertEqual(result["state"], "MORE_EVIDENCE_REQUIRED")
            self.assertIn("insufficient_event_clusters", result["reason_codes"])
            lines = samples.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[1]); row["gross_pnl_units"] = 999
            lines[1] = json.dumps(row, sort_keys=True)
            samples.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(scorecard.ScorecardError, "record_hash_mismatch"):
                scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                    attestation_trust_registry=trust)

    def test_short_forward_window_cannot_reach_economic_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key, public_key = key_pair(root)
            report = verified_report(3_000, private_key=private_key, public_key=public_key)
            trust = trust_registry(root, report)
            samples = root / "samples.jsonl"
            write_samples(samples, report_sha=report["report_sha256"], spacing_ms=1)
            result = scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                         attestation_trust_registry=trust)
            self.assertEqual(result["state"], "MORE_EVIDENCE_REQUIRED")
            self.assertIn("insufficient_forward_duration", result["reason_codes"])

    def test_expected_shortfall_tail_limit_is_a_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key, public_key = key_pair(root)
            pnl_values = [100] * 29 + [-2_000]
            report = verified_report(sum(pnl_values), private_key=private_key, public_key=public_key)
            trust = trust_registry(root, report)
            samples = root / "samples.jsonl"
            write_samples(samples, report_sha=report["report_sha256"], pnl_values=pnl_values)
            result = scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                         attestation_trust_registry=trust)
            self.assertEqual(result["state"], "MORE_EVIDENCE_REQUIRED")
            self.assertLess(result["risk"]["expected_shortfall_95"], -0.05)
            self.assertIn("expected_shortfall_limit_not_met", result["reason_codes"])

    def test_spliced_report_or_attestation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private_key, public_key = key_pair(Path(directory))
            report = verified_report(3_000, private_key=private_key, public_key=public_key)
            trust = trust_registry(Path(directory), report)
            samples = Path(directory) / "samples.jsonl"
            write_samples(samples, report_sha=report["report_sha256"])
            report["reconstructed_realized_pnl_units"] = 3_001
            with self.assertRaisesRegex(scorecard.ScorecardError, "report_hash_mismatch"):
                scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                    attestation_trust_registry=trust)

    def test_forged_hmac_or_signature_cannot_reach_economic_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_key, public_key = key_pair(root)
            report = verified_report(3_000, private_key=private_key, public_key=public_key)
            trust = trust_registry(root, report)
            samples = root / "samples.jsonl"
            write_samples(samples, report_sha=report["report_sha256"])
            report["attestation"]["algorithm"] = "HMAC-SHA256"
            with self.assertRaisesRegex(scorecard.ScorecardError, "attestation_identity"):
                scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                    attestation_trust_registry=trust)
            report = verified_report(3_000, private_key=private_key, public_key=public_key)
            report["attestation"]["signature_base64"] = "AA=="
            with self.assertRaisesRegex(scorecard.ScorecardError, "attestation_signature_invalid"):
                scorecard.scorecard(report, samples, attestation_public_key=public_key,
                                    attestation_trust_registry=trust)


if __name__ == "__main__":
    unittest.main()
