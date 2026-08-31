#!/usr/bin/env python3
"""Read-only verification of V7 PnL attestations without signing authority."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import v7_real_pnl_verifier as verifier


class AttestationVerificationError(ValueError):
    pass


PUBLIC_ATTESTATION_SCHEMA = "polymarket_v7_real_pnl_attestation_v3"
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _public_payload(attestation: dict[str, Any]) -> dict[str, Any]:
    return {key: attestation[key] for key in (
        "schema", "issued_at", "operator_id", "report_sha256", "model_sha", "journal_head_hash", "public_key_sha256",
    )}


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise AttestationVerificationError(f"{field}:invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttestationVerificationError(f"{field}:invalid") from exc
    if parsed.tzinfo is None:
        raise AttestationVerificationError(f"{field}:timezone")
    return parsed.astimezone(timezone.utc)


def _trusted_attestors(path: Path) -> list[dict[str, Any]]:
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttestationVerificationError("trust_registry_unreadable") from exc
    expected = {"schema", "automatic_promotion", "trusted_attestors", "note"}
    rows = registry.get("trusted_attestors") if isinstance(registry, dict) else None
    if (not isinstance(registry, dict) or set(registry) != expected
            or registry.get("schema") != "polymarket_v7_attestation_trust_registry_v1"
            or registry.get("automatic_promotion") is not False
            or not isinstance(registry.get("note"), str)
            or not isinstance(rows, list)):
        raise AttestationVerificationError("trust_registry_shape")
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"operator_id", "public_key_sha256", "not_before", "not_after"}:
            raise AttestationVerificationError("trust_registry_attestor")
        identity = (row.get("operator_id"), row.get("public_key_sha256"))
        if (not isinstance(identity[0], str) or not identity[0].strip()
                or not isinstance(identity[1], str) or not SHA256_RE.fullmatch(identity[1])
                or identity in seen):
            raise AttestationVerificationError("trust_registry_attestor")
        before, after = _timestamp(row.get("not_before"), "trust_not_before"), _timestamp(row.get("not_after"), "trust_not_after")
        if before > after:
            raise AttestationVerificationError("trust_registry_window")
        seen.add(identity)
    return rows


def _require_trusted_attestor(attestation: dict[str, Any], registry_path: Path) -> None:
    issued = _timestamp(attestation.get("issued_at"), "attestation_issued_at")
    for row in _trusted_attestors(registry_path):
        if (row["operator_id"] == attestation["operator_id"]
                and row["public_key_sha256"] == attestation["public_key_sha256"]
                and _timestamp(row["not_before"], "trust_not_before") <= issued <= _timestamp(row["not_after"], "trust_not_after")):
            return
    raise AttestationVerificationError("untrusted_attestor")


def verify_public(report: Any, *, public_key: Path, trust_registry: Path) -> dict[str, Any]:
    """Verify the detached RSA attestation without reading a signing secret.

    This intentionally repeats the small report/attestation contract rather
    than invoking the scorecard, which keeps public verification independent
    from economic scoring and incapable of promotion.
    """
    if (not isinstance(report, dict) or report.get("state") != "REAL_PNL_VERIFIED"
            or report.get("real_pnl_verified") is not True
            or not SHA40_RE.fullmatch(str(report.get("model_sha")))
            or not SHA256_RE.fullmatch(str(report.get("report_sha256")))):
        raise AttestationVerificationError("public_report_identity")
    attestation = report.get("attestation")
    required = {"schema", "issued_at", "operator_id", "report_sha256", "model_sha", "journal_head_hash", "algorithm",
                "public_key_sha256", "signature_base64"}
    if not isinstance(attestation, dict) or set(attestation) != required:
        raise AttestationVerificationError("public_attestation_shape")
    if (attestation.get("schema") != PUBLIC_ATTESTATION_SCHEMA
            or _timestamp(attestation.get("issued_at"), "attestation_issued_at") is None
            or not isinstance(attestation.get("operator_id"), str) or not attestation["operator_id"].strip()
            or attestation.get("report_sha256") != report["report_sha256"]
            or attestation.get("model_sha") != report["model_sha"]
            or not SHA256_RE.fullmatch(str(attestation.get("journal_head_hash")))
            or attestation.get("algorithm") != "RSA-SHA256"
            or not SHA256_RE.fullmatch(str(attestation.get("public_key_sha256")))
            or not isinstance(attestation.get("signature_base64"), str)):
        raise AttestationVerificationError("public_attestation_identity")
    unsigned = dict(report)
    unsigned.pop("attestation")
    unsigned.pop("report_sha256")
    unsigned["state"] = "REAL_PNL_RECONCILED_UNSIGNED"
    unsigned["real_pnl_verified"] = False
    if verifier.digest(unsigned) != report["report_sha256"]:
        raise AttestationVerificationError("public_report_hash")
    public_key = Path(public_key)
    if not public_key.is_file():
        raise AttestationVerificationError("public_key_missing")
    if hashlib.sha256(public_key.read_bytes()).hexdigest() != attestation["public_key_sha256"]:
        raise AttestationVerificationError("public_key_identity")
    _require_trusted_attestor(attestation, trust_registry)
    try:
        signature = base64.b64decode(attestation["signature_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise AttestationVerificationError("public_signature_encoding") from exc
    if not signature:
        raise AttestationVerificationError("public_signature_encoding")
    with tempfile.TemporaryDirectory(prefix="v7-public-attestation-") as directory:
        signature_path = Path(directory) / "signature.bin"
        signature_path.write_bytes(signature)
        completed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public_key), "-signature", str(signature_path)],
            input=_canonical(_public_payload(attestation)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode != 0:
        raise AttestationVerificationError("public_signature_invalid")
    return {"schema_version": 1, "state": "PUBLIC_ATTESTATION_VERIFIED",
            "model_sha": report["model_sha"], "report_sha256": report["report_sha256"],
            "public_key_sha256": attestation["public_key_sha256"], "issued_at": attestation["issued_at"],
            "automatic_promotion": False}


def verify_package(path: Path, *, signing_key: str) -> dict[str, Any]:
    path = Path(path)
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        signature = json.loads((path / "signature.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttestationVerificationError("package_unreadable") from exc
    supplied = manifest.pop("manifest_sha256", None)
    if not isinstance(supplied, str) or hashlib.sha256(_canonical(manifest)).hexdigest() != supplied:
        raise AttestationVerificationError("manifest_hash")
    hashes = manifest.get("package_files")
    if not isinstance(hashes, dict) or not hashes:
        raise AttestationVerificationError("package_files")
    for name, expected in hashes.items():
        try: actual = hashlib.sha256((path / name).read_bytes()).hexdigest()
        except OSError as exc: raise AttestationVerificationError("package_file_missing") from exc
        if actual != expected: raise AttestationVerificationError("package_file_hash")
    payload = {"schema": signature.get("schema"), "operator_id": signature.get("operator_id"), "manifest_sha256": signature.get("manifest_sha256"), "model_sha": signature.get("model_sha")}
    expected_signature = hmac.new(signing_key.encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest()
    if signature.get("manifest_sha256") != supplied or not hmac.compare_digest(expected_signature, str(signature.get("signature", ""))):
        raise AttestationVerificationError("signature_mismatch")
    return {"schema_version": 1, "state": "ATTESTATION_VERIFIED", "manifest_sha256": supplied, "model_sha": manifest.get("model_sha"), "automatic_promotion": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-report", type=Path, required=True,
                        help="RSA-attested report to verify without a secret")
    parser.add_argument("--public-key", type=Path, required=True,
                        help="public key paired with the RSA attestation")
    parser.add_argument("--trust-registry", type=Path, required=True,
                        help="time-bounded public-key trust registry")
    args = parser.parse_args()
    result = verify_public(json.loads(args.public_report.read_text(encoding="utf-8")), public_key=args.public_key,
                           trust_registry=args.trust_registry)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
