#!/usr/bin/env python3
"""Decrypt a short-lived AWS session envelope with the deployment SSH key.

The envelope may be committed because it contains only ciphertext. The SSH
private key stays in GitHub Secrets. Plain AWS credentials are never printed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SCHEMA = "polymarket_v7_aws_session_envelope_v1"
PLAIN_SCHEMA = "polymarket_v7_aws_session_plaintext_v1"
ALGORITHM = "ED25519_TO_X25519_HKDF_SHA256_AES256GCM"
AAD = b"polymarket-v7-aws-session-envelope-v1"


class EnvelopeError(ValueError):
    pass


def b64(value: object, name: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise EnvelopeError(name)
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise EnvelopeError(name) from exc


def ed25519_public_to_x25519(raw: bytes) -> bytes:
    if len(raw) != 32:
        raise EnvelopeError("ed25519_public_length")
    p = 2**255 - 19
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    denominator = (1 - y) % p
    if denominator == 0:
        raise EnvelopeError("invalid_ed25519_public")
    u = ((1 + y) * pow(denominator, p - 2, p)) % p
    return u.to_bytes(32, "little")


def ed25519_private_to_x25519(private_key: ed25519.Ed25519PrivateKey) -> x25519.X25519PrivateKey:
    seed = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    digest = bytearray(hashlib.sha512(seed).digest()[:32])
    digest[0] &= 248
    digest[31] &= 127
    digest[31] |= 64
    return x25519.X25519PrivateKey.from_private_bytes(bytes(digest))


def decrypt(envelope_path: Path, ssh_private_path: Path, now: int | None = None) -> dict[str, str]:
    now = int(time.time()) if now is None else int(now)
    if envelope_path.is_symlink() or ssh_private_path.is_symlink():
        raise EnvelopeError("symlink_not_allowed")
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict):
        raise EnvelopeError("envelope_object_required")
    if envelope.get("schema") != SCHEMA or envelope.get("version") != 1:
        raise EnvelopeError("envelope_schema")
    if envelope.get("algorithm") != ALGORITHM:
        raise EnvelopeError("envelope_algorithm")
    if envelope.get("paper_only") is not True:
        raise EnvelopeError("paper_only_required")
    if envelope.get("authenticated_execution") is not False:
        raise EnvelopeError("authenticated_execution_must_remain_false")
    if envelope.get("real_order_submission") is not False:
        raise EnvelopeError("real_order_submission_must_remain_false")
    not_after = envelope.get("not_after_unix")
    if type(not_after) is not int or not_after <= now:
        raise EnvelopeError("envelope_expired")

    private = serialization.load_ssh_private_key(
        ssh_private_path.read_bytes(), password=None
    )
    if not isinstance(private, ed25519.Ed25519PrivateKey):
        raise EnvelopeError("deployment_key_must_be_ed25519")
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if hashlib.sha256(public_raw).hexdigest() != envelope.get("recipient_sha256"):
        raise EnvelopeError("recipient_key_mismatch")

    # Check the conversion on both sides before deriving the session key.
    expected_xpub = ed25519_public_to_x25519(public_raw)
    xprivate = ed25519_private_to_x25519(private)
    actual_xpub = xprivate.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if actual_xpub != expected_xpub:
        raise EnvelopeError("ed25519_x25519_conversion_mismatch")

    ephemeral = x25519.X25519PublicKey.from_public_bytes(
        b64(envelope.get("ephemeral_x25519_public_b64"), "ephemeral_public")
    )
    salt = b64(envelope.get("salt_b64"), "salt")
    nonce = b64(envelope.get("nonce_b64"), "nonce")
    ciphertext = b64(envelope.get("ciphertext_b64"), "ciphertext")
    if len(salt) != 16 or len(nonce) != 12:
        raise EnvelopeError("envelope_nonce_or_salt")

    shared = xprivate.exchange(ephemeral)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=AAD,
    ).derive(shared)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, AAD)
        value = json.loads(plaintext)
    except Exception as exc:
        raise EnvelopeError("envelope_decryption_failed") from exc
    if not isinstance(value, dict) or value.get("schema") != PLAIN_SCHEMA:
        raise EnvelopeError("plaintext_schema")
    if value.get("region") != "eu-west-2":
        raise EnvelopeError("wrong_region")
    plain_expiry = value.get("not_after_unix")
    if type(plain_expiry) is not int or plain_expiry <= now or plain_expiry > not_after:
        raise EnvelopeError("plaintext_expiry")

    out = {
        "AWS_ACCESS_KEY_ID": value.get("aws_access_key_id"),
        "AWS_SECRET_ACCESS_KEY": value.get("aws_secret_access_key"),
        "AWS_SESSION_TOKEN": value.get("aws_session_token"),
        "AWS_REGION": "eu-west-2",
        "AWS_DEFAULT_REGION": "eu-west-2",
    }
    if not isinstance(out["AWS_ACCESS_KEY_ID"], str) or not out["AWS_ACCESS_KEY_ID"].startswith("ASIA"):
        raise EnvelopeError("access_key")
    for name in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        if not isinstance(out[name], str) or not out[name]:
            raise EnvelopeError(name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--ssh-private-key", type=Path, required=True)
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        values = decrypt(args.envelope, args.ssh_private_key)
    except (OSError, json.JSONDecodeError, EnvelopeError) as exc:
        parser.exit(2, f"v7_aws_envelope: {exc}\n")

    # Mask before the values enter the GitHub environment file.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        print(f"::add-mask::{values[name]}")
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8") as handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")
            handle.write("V7_AWS_ENVELOPE_READY=true\n")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write("ready=true\n")
    print(json.dumps({
        "schema": "polymarket_v7_aws_session_decryption_receipt_v1",
        "region": "eu-west-2",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "credentials_printed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
