#!/usr/bin/env python3
"""Read-only HMAC verification for a V7 PnL attestation; keys are env-only."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

import v7_real_pnl_verifier as verifier


class AttestationVerificationError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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


def verify(report: dict, *, signing_key_env: str) -> bool:
    attestation = report.get("attestation")
    if not isinstance(attestation, dict) or report.get("state") != "REAL_PNL_VERIFIED" or report.get("real_pnl_verified") is not True:
        return False
    key = os.environ.get(signing_key_env, "")
    if not key:
        return False
    unsigned = dict(report); unsigned.pop("attestation", None); unsigned.pop("report_sha256", None)
    unsigned["state"] = "REAL_PNL_RECONCILED_UNSIGNED"; unsigned["real_pnl_verified"] = False
    if verifier.digest(unsigned) != attestation.get("report_sha256"):
        return False
    payload = {key: attestation.get(key) for key in ("schema", "operator_id", "report_sha256", "model_sha", "journal_head_hash")}
    expected = hmac.new(key.encode("utf-8"), verifier.canonical_bytes(payload), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(attestation.get("signature", "")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--signing-key-env", default="V7_ATTESTATION_HMAC_KEY")
    args = parser.parse_args()
    return 0 if verify(json.loads(args.report.read_text(encoding="utf-8")), signing_key_env=args.signing_key_env) else 1


if __name__ == "__main__":
    raise SystemExit(main())
