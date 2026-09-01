#!/usr/bin/env python3
"""Validate redacted V7 session coverage metadata for real-PnL reconciliation.

Only SHA-256 session identifiers and evidence hashes are allowed.  This module
does not create session keys, read credentials, authenticate, or make network
requests; it makes an omitted session visible to the independent verifier.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "polymarket_v7_session_registry_v1"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SESSION_FIELDS = {"session_key_id_hash", "activated_at_ms", "retired_at_ms", "registration_evidence_hash"}


class SessionRegistryError(ValueError):
    pass


def validate(value: Any, *, expected_model_sha: str | None = None) -> dict[str, Any]:
    required = {"schema", "model_sha", "wallet_id_hash", "sessions", "registry_evidence_hash"}
    if not isinstance(value, dict) or set(value) != required or value.get("schema") != SCHEMA:
        raise SessionRegistryError("registry_shape")
    model_sha = value.get("model_sha")
    if not isinstance(model_sha, str) or not SHA40.fullmatch(model_sha) or (expected_model_sha is not None and model_sha != expected_model_sha):
        raise SessionRegistryError("registry_model_sha")
    if (not isinstance(value.get("wallet_id_hash"), str) or not SHA256.fullmatch(value["wallet_id_hash"])
            or not isinstance(value.get("registry_evidence_hash"), str) or not SHA256.fullmatch(value["registry_evidence_hash"])):
        raise SessionRegistryError("registry_identity")
    sessions = value.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise SessionRegistryError("registry_sessions")
    previous: str | None = None
    for session in sessions:
        if not isinstance(session, dict) or set(session) != SESSION_FIELDS:
            raise SessionRegistryError("session_shape")
        session_hash = session.get("session_key_id_hash")
        activated, retired = session.get("activated_at_ms"), session.get("retired_at_ms")
        if (not isinstance(session_hash, str) or not SHA256.fullmatch(session_hash)
                or previous is not None and session_hash <= previous
                or isinstance(activated, bool) or not isinstance(activated, int) or activated <= 0
                or retired is not None and (isinstance(retired, bool) or not isinstance(retired, int) or retired <= activated)
                or not isinstance(session.get("registration_evidence_hash"), str)
                or not SHA256.fullmatch(session["registration_evidence_hash"])):
            raise SessionRegistryError("session_identity")
        previous = session_hash
    return value


def load(path: Path, *, expected_model_sha: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionRegistryError("registry_unreadable") from exc
    return validate(value, expected_model_sha=expected_model_sha)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--model-sha")
    args = parser.parse_args()
    value = load(args.registry, expected_model_sha=args.model_sha)
    print(json.dumps({"schema": SCHEMA, "model_sha": value["model_sha"], "session_count": len(value["sessions"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
