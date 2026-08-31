from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHA = "e" * 40


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verifier = load("v7_real_pnl_verifier")
generator = load("v7_generate_pnl_attestation")
package_verifier = load("v7_verify_pnl_attestation")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report(root: Path) -> dict:
    ledger = root / "ledger.jsonl"
    ledger.write_text(json.dumps({
        "record_kind": "ECONOMIC_JOURNAL", "model_sha": SHA, "entry_id": "entry-1", "entry_hash": "a" * 64,
        "entry_type": "TOKEN_REDEEM", "source": "POLYGON_RPC", "source_record_id": "private-redeem-id", "observed_ts_ms": 1,
    }) + "\n", encoding="utf-8")
    evidence = root / "evidence.jsonl"; evidence.write_text("{}\n", encoding="utf-8")
    provenance = root / "provenance.jsonl"; provenance.write_text("{}\n", encoding="utf-8")
    unsigned = {
        "schema": verifier.SCHEMA, "state": "REAL_PNL_RECONCILED_UNSIGNED", "real_pnl_verified": False,
        "model_sha": SHA, "journal_head_hash": "b" * 64, "journal_entries": 1,
        "ledger_path": str(ledger), "ledger_sha256": sha256(ledger),
        "evidence_tape_path": str(evidence), "evidence_tape_sha256": sha256(evidence),
        "provenance_tape_path": str(provenance), "provenance_tape_sha256": sha256(provenance),
        "reconstructed_realized_pnl_units": 25, "open_outcome_positions": {}, "reason_codes": [],
    }
    unsigned["report_sha256"] = verifier.digest(unsigned)
    return unsigned


class PnlAttestationPackageTests(unittest.TestCase):
    def test_signed_redacted_package_is_independently_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = generator.build_package(
                report(root), root / "package", operator_id="audit-operator", signing_key="test-key",
                created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
            )
            self.assertEqual(manifest["schema"], generator.SCHEMA)
            self.assertFalse(manifest["automatic_promotion"])
            result = package_verifier.verify_package(root / "package", signing_key="test-key")
            self.assertEqual(result["state"], "ATTESTATION_VERIFIED")
            journal = (root / "package" / "journal.csv").read_text(encoding="utf-8")
            self.assertNotIn("private-redeem-id", journal)

    def test_tampered_package_or_wrong_key_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generator.build_package(report(root), root / "package", operator_id="audit-operator", signing_key="test-key")
            with self.assertRaisesRegex(package_verifier.AttestationVerificationError, "signature_mismatch"):
                package_verifier.verify_package(root / "package", signing_key="wrong-key")
            path = root / "package" / "journal.csv"
            path.write_text(path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(package_verifier.AttestationVerificationError, "package_file_hash"):
                package_verifier.verify_package(root / "package", signing_key="test-key")

    def test_only_reconciled_unsigned_report_can_be_attested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = report(root)
            value["reason_codes"] = ["unresolved_break"]
            with self.assertRaisesRegex(generator.AttestationPackageError, "reconciled_unsigned_required|report:identity"):
                generator.build_package(value, root / "package", operator_id="audit-operator", signing_key="test-key")


if __name__ == "__main__":
    unittest.main()
