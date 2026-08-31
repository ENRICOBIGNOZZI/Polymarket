#!/usr/bin/env python3
"""Create a publicly verifiable V7 real-PnL attestation from a reconciled report."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from io import StringIO
from typing import Any

import v7_real_pnl_verifier as verifier

SCHEMA = "polymarket_v7_pnl_attestation_package_v1"
SIGNATURE_SCHEMA = "polymarket_v7_pnl_attestation_package_signature_v1"
PUBLIC_ATTESTATION_SCHEMA = "polymarket_v7_real_pnl_attestation_v3"


class AttestationPackageError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _report_identity(report: dict[str, Any]) -> None:
    if not isinstance(report, dict) or report.get("state") != "REAL_PNL_RECONCILED_UNSIGNED" or report.get("real_pnl_verified") is not False or report.get("reason_codes") != []:
        raise AttestationPackageError("reconciled_unsigned_required")
    supplied = report.get("report_sha256")
    unsigned = dict(report); unsigned.pop("report_sha256", None)
    if not isinstance(supplied, str) or verifier.digest(unsigned) != supplied:
        raise AttestationPackageError("report:identity")


def _public_attestation_identity(report: dict[str, Any]) -> None:
    """Validate the exact unsigned report that a public signature will bind."""
    if (not isinstance(report, dict)
            or report.get("state") != "REAL_PNL_RECONCILED_UNSIGNED"
            or report.get("real_pnl_verified") is not False
            or report.get("reason_codes") != []):
        raise AttestationPackageError("reconciled_unsigned_required")
    supplied = report.get("report_sha256")
    unsigned = dict(report)
    unsigned.pop("report_sha256", None)
    if (not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied)
            or verifier.digest(unsigned) != supplied
            or not isinstance(report.get("model_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", str(report.get("model_sha")))
            or not isinstance(report.get("journal_head_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("journal_head_hash")))):
        raise AttestationPackageError("report:identity")


def public_attestation(report: dict[str, Any], *, operator_id: str,
                       signing_key: Path, public_key: Path,
                       issued_at: datetime | None = None) -> dict[str, Any]:
    """Create a detached RSA signature that a read-only scorecard can verify.

    The private key is provided only as a runtime file to OpenSSL and is never
    copied into the package or repository.  The public-key digest prevents a
    signature from being re-bound to a different verification identity.
    """
    _public_attestation_identity(report)
    if not isinstance(operator_id, str) or not operator_id.strip():
        raise AttestationPackageError("operator_or_key_missing")
    signing_key, public_key = Path(signing_key), Path(public_key)
    if not signing_key.is_file() or not public_key.is_file():
        raise AttestationPackageError("public_signing_key_missing")
    issued_at = issued_at or datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        raise AttestationPackageError("issued_at_timezone")
    payload = {
        "schema": PUBLIC_ATTESTATION_SCHEMA,
        "issued_at": issued_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "operator_id": operator_id.strip(),
        "report_sha256": report["report_sha256"],
        "model_sha": report["model_sha"],
        "journal_head_hash": report["journal_head_hash"],
        "public_key_sha256": _hash(public_key.read_bytes()),
    }
    completed = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(signing_key)],
        input=_canonical(payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        raise AttestationPackageError("public_signature_failed")
    return {**payload, "algorithm": "RSA-SHA256",
            "signature_base64": base64.b64encode(completed.stdout).decode("ascii")}


def _redacted_journal(report: dict[str, Any]) -> bytes:
    ledger_path = Path(str(report.get("ledger_path", "")))
    if not ledger_path.is_file() or _hash(ledger_path.read_bytes()) != report.get("ledger_sha256"):
        raise AttestationPackageError("ledger:identity")
    stream = StringIO(); writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(("entry_id", "entry_hash", "entry_type", "source", "observed_ts_ms"))
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        writer.writerow((row.get("entry_id", ""), row.get("entry_hash", ""), row.get("entry_type", ""), row.get("source", ""), row.get("observed_ts_ms", "")))
    return stream.getvalue().encode("utf-8")


def build_package(report: dict[str, Any], output: Path, *, operator_id: str, signing_key: str,
                  created_at: datetime | None = None) -> dict[str, Any]:
    """Create an immutable redacted attestation package from a reconciled report."""
    _report_identity(report)
    if not isinstance(operator_id, str) or not operator_id or not signing_key:
        raise AttestationPackageError("operator_or_key_missing")
    output = Path(output)
    if output.exists():
        raise AttestationPackageError("package_path_exists")
    created_at = created_at or datetime.now(timezone.utc)
    if created_at.tzinfo is None: raise AttestationPackageError("created_at_timezone")
    redacted_report = {key: value for key, value in report.items() if key not in {"ledger_path", "evidence_tape_path", "provenance_tape_path"}}
    sources = {key: value for key, value in report.items() if key.endswith("_sha256")}
    software = {"exact_code_sha": report["model_sha"], "report_sha256": report["report_sha256"], "verifier_schema": report.get("schema")}
    files = {
        "pnl_report.json": _canonical(redacted_report) + b"\n",
        "journal.csv": _redacted_journal(report),
        "positions.csv": b"asset,base_units\n",
        "fills.csv": b"fill_id,order_id,size_base_units\n",
        "cash_flows.csv": b"entry_id,asset,base_units\n",
        "reconciliation_breaks.csv": b"kind,detail\n",
        "source_hashes.json": _canonical(sources) + b"\n",
        "software_manifest.json": _canonical(software) + b"\n",
    }
    output.mkdir(parents=True)
    for name, data in files.items(): (output / name).write_bytes(data)
    manifest = {"schema": SCHEMA, "created_at": created_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"), "operator_id": operator_id, "model_sha": report["model_sha"], "report_sha256": report["report_sha256"], "automatic_promotion": False, "package_files": {name: _hash(data) for name, data in files.items()}}
    manifest["manifest_sha256"] = _hash(_canonical(manifest))
    (output / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
    payload = {"schema": SIGNATURE_SCHEMA, "operator_id": operator_id, "manifest_sha256": manifest["manifest_sha256"], "model_sha": report["model_sha"]}
    signature = {**payload, "algorithm": "HMAC-SHA256", "signature": hmac.new(signing_key.encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest()}
    (output / "signature.json").write_bytes(_canonical(signature) + b"\n")
    return manifest


def generate(report: dict, *, operator_id: str, signing_key_env: str) -> dict:
    """Retired symmetric-signature entry point; it cannot make a PnL claim."""
    _ = report, operator_id, signing_key_env
    raise AttestationPackageError("public_signature_required")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--public-signing-key", type=Path, required=True,
                        help="runtime-only RSA private key for a publicly verifiable V2 attestation")
    parser.add_argument("--attestation-public-key", type=Path, required=True,
                        help="RSA public key paired with --public-signing-key")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    attestation = public_attestation(report, operator_id=args.operator_id,
                                     signing_key=args.public_signing_key,
                                     public_key=args.attestation_public_key)
    generated = {**report, "attestation": attestation, "real_pnl_verified": True,
                 "state": "REAL_PNL_VERIFIED"}
    args.output.write_text(json.dumps(generated, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
