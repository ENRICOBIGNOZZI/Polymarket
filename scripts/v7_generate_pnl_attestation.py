#!/usr/bin/env python3
"""Attach an HMAC attestation to a reconciled report using an environment key."""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from io import StringIO
from typing import Any

import v7_real_pnl_verifier as verifier

SCHEMA = "polymarket_v7_pnl_attestation_package_v1"
SIGNATURE_SCHEMA = "polymarket_v7_pnl_attestation_package_signature_v1"


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
    key = os.environ.get(signing_key_env, "")
    attestation = verifier.attest(report, operator_id=operator_id, signing_key=key)
    return {**report, "attestation": attestation, "real_pnl_verified": True,
            "state": "REAL_PNL_VERIFIED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--signing-key-env", default="V7_ATTESTATION_HMAC_KEY")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    generated = generate(report, operator_id=args.operator_id, signing_key_env=args.signing_key_env)
    args.output.write_text(json.dumps(generated, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
