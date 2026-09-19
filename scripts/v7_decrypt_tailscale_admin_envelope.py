#!/usr/bin/env python3
"""Decrypt a short-lived Tailscale admin credential envelope.

The ciphertext can live on an isolated branch. The deployment SSH private key
remains in GitHub Secrets and plaintext credentials are never printed.
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

SCHEMA = "polymarket_v7_tailscale_admin_envelope_v1"
PLAIN_SCHEMA = "polymarket_v7_tailscale_admin_plaintext_v1"
ALGORITHM = "ED25519_TO_X25519_HKDF_SHA256_AES256GCM"
AAD = b"polymarket-v7-tailscale-admin-envelope-v1"


class EnvelopeError(ValueError):
    pass


def decode(value: object, name: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise EnvelopeError(name)
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:
        raise EnvelopeError(name) from exc


def xprivate_from_ed25519(private: ed25519.Ed25519PrivateKey) -> x25519.X25519PrivateKey:
    seed = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    scalar = bytearray(hashlib.sha512(seed).digest()[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return x25519.X25519PrivateKey.from_private_bytes(bytes(scalar))


def decrypt(envelope_path: Path, private_path: Path, now: int | None = None) -> dict[str, str]:
    now = int(time.time()) if now is None else int(now)
    if envelope_path.is_symlink() or private_path.is_symlink():
        raise EnvelopeError("symlink_not_allowed")
    env = json.loads(envelope_path.read_text(encoding="utf-8"))
    if not isinstance(env, dict) or env.get("schema") != SCHEMA or env.get("version") != 1:
        raise EnvelopeError("envelope_schema")
    if env.get("algorithm") != ALGORITHM:
        raise EnvelopeError("envelope_algorithm")
    expiry = env.get("not_after_unix")
    if type(expiry) is not int or expiry <= now:
        raise EnvelopeError("envelope_expired")

    private = serialization.load_ssh_private_key(private_path.read_bytes(), password=None)
    if not isinstance(private, ed25519.Ed25519PrivateKey):
        raise EnvelopeError("deployment_key_must_be_ed25519")
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if hashlib.sha256(public_raw).hexdigest() != env.get("recipient_sha256"):
        raise EnvelopeError("recipient_key_mismatch")

    xprivate = xprivate_from_ed25519(private)
    ephemeral = x25519.X25519PublicKey.from_public_bytes(
        decode(env.get("ephemeral_x25519_public_b64"), "ephemeral_public")
    )
    salt = decode(env.get("salt_b64"), "salt")
    nonce = decode(env.get("nonce_b64"), "nonce")
    ciphertext = decode(env.get("ciphertext_b64"), "ciphertext")
    if len(salt) != 16 or len(nonce) != 12:
        raise EnvelopeError("nonce_or_salt")
    shared = xprivate.exchange(ephemeral)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=AAD).derive(shared)
    try:
        plain = json.loads(AESGCM(key).decrypt(nonce, ciphertext, AAD))
    except Exception as exc:
        raise EnvelopeError("decryption_failed") from exc
    if not isinstance(plain, dict) or plain.get("schema") != PLAIN_SCHEMA:
        raise EnvelopeError("plaintext_schema")
    if type(plain.get("not_after_unix")) is not int or plain["not_after_unix"] <= now:
        raise EnvelopeError("plaintext_expired")
    email = plain.get("email")
    password = plain.get("password")
    if not isinstance(email, str) or "@" not in email or not isinstance(password, str) or not password:
        raise EnvelopeError("credentials")
    return {"V7_TS_ADMIN_EMAIL": email, "V7_TS_ADMIN_PASSWORD": password}


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
        parser.exit(2, f"v7_tailscale_admin_envelope: {exc}\n")
    for value in values.values():
        print(f"::add-mask::{value}")
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8") as handle:
            for name, value in values.items():
                handle.write(f"{name}={value}\n")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write("ready=true\n")
    print(json.dumps({
        "schema": "polymarket_v7_tailscale_admin_decryption_receipt_v1",
        "credentials_printed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
