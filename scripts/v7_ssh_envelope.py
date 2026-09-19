#!/usr/bin/env python3
"""Decrypt a bounded V7 envelope addressed to an SSH Ed25519 key."""
from __future__ import annotations
import argparse, base64, hashlib, json, os
from pathlib import Path
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SCHEMA = "polymarket_v7_ssh_envelope_v1"
EXPECTED_KEYS = {
    "schema","recipient_type","recipient_fingerprint","ephemeral_public_b64",
    "salt_b64","nonce_b64","ciphertext_b64",
}

def fail(msg: str) -> None:
    raise ValueError(msg)

def b64(value: object, name: str, exact: int | None = None, maximum: int = 65536) -> bytes:
    if not isinstance(value, str) or len(value) > maximum * 2:
        fail(name)
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError(name) from exc
    if exact is not None and len(raw) != exact:
        fail(name)
    if len(raw) > maximum:
        fail(name)
    return raw

def ed_private_to_x25519(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    scalar = bytearray(h[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return bytes(scalar)

def decrypt(private_key: bytes, envelope: dict[str, object]) -> bytes:
    if not isinstance(envelope, dict) or set(envelope) != EXPECTED_KEYS:
        fail("envelope_fields")
    if envelope.get("schema") != SCHEMA or envelope.get("recipient_type") != "ssh-ed25519":
        fail("envelope_schema")
    key = serialization.load_ssh_private_key(private_key, password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        fail("recipient_key_type")
    public_line = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    fingerprint = hashlib.sha256(public_line.strip()).hexdigest()
    if envelope.get("recipient_fingerprint") != fingerprint:
        fail("recipient_fingerprint")
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    xpriv = x25519.X25519PrivateKey.from_private_bytes(ed_private_to_x25519(seed))
    eph = x25519.X25519PublicKey.from_public_bytes(
        b64(envelope["ephemeral_public_b64"], "ephemeral_public", exact=32)
    )
    salt = b64(envelope["salt_b64"], "salt", exact=16)
    nonce = b64(envelope["nonce_b64"], "nonce", exact=12)
    ciphertext = b64(envelope["ciphertext_b64"], "ciphertext")
    info = (SCHEMA + ":" + fingerprint).encode()
    shared = xpriv.exchange(eph)
    aead_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt, info=info
    ).derive(shared)
    return ChaCha20Poly1305(aead_key).decrypt(nonce, ciphertext, info)

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--private-key", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    for path in (a.private_key, a.input):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
            p.error("unsafe input")
    try:
        envelope = json.loads(a.input.read_text(encoding="utf-8"))
        plaintext = decrypt(a.private_key.read_bytes(), envelope)
        value = json.loads(plaintext)
        if not isinstance(value, dict):
            raise ValueError("payload")
    except Exception as exc:
        p.exit(2, f"v7_ssh_envelope: {type(exc).__name__}\n")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(plaintext)
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    print("decrypt_result=success")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
