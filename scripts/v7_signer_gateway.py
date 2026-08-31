#!/usr/bin/env python3
"""Fail-closed isolated signer admission boundary.

It has no private-key input and intentionally cannot sign or submit orders.
Private deployment may replace only the final signer adapter after its own
operational controls; this checked-in gateway never grants value authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable

from v7_approval_envelope import ApprovalEnvelopeError, authorize_intent, envelope_hash, validate_structure, verify_signature


SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LIVE_MODES = {"MICRO_LIVE", "LIVE_RESTRICTED", "LIVE_SCALED"}


@dataclass
class SignerGateway:
    last_sequence: int = 0
    audit: list[dict[str, Any]] = field(default_factory=list)

    def admit(self, intent: dict[str, Any], *, envelope: dict[str, Any] | None = None,
              signature_verifier: Callable[[bytes, str, str], bool] | None = None,
              now: datetime | None = None) -> dict[str, Any]:
        instant = now or datetime.now(timezone.utc)
        reason = "CHECKED_IN_LIVE_CAPS_ZERO"
        approval_hash = None
        try:
            required = {"intent_sequence", "exact_code_sha", "build_manifest_hash", "config_bundle_hash", "policy_hash", "execution_mode", "condition_id", "token_id", "order_type", "post_only", "size_base_units", "gross_exposure_base_units", "event_loss_base_units", "daily_loss_base_units", "open_order_count"}
            if not isinstance(intent, dict) or set(intent) != required: raise ValueError("intent_shape")
            sequence = intent["intent_sequence"]
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= self.last_sequence: raise ValueError("intent_sequence")
            if not SHA40.fullmatch(str(intent["exact_code_sha"])) or any(not SHA256.fullmatch(str(intent[key])) for key in ("build_manifest_hash", "config_bundle_hash", "policy_hash")): raise ValueError("intent_identity")
            if intent["execution_mode"] not in LIVE_MODES: raise ValueError("intent_mode")
            if not all(isinstance(intent[key], str) and intent[key] for key in ("condition_id", "token_id")): raise ValueError("intent_market")
            if intent["order_type"] not in {"GTC", "GTD", "FOK", "FAK"} or intent["post_only"] is not True: raise ValueError("intent_type")
            if isinstance(intent["size_base_units"], bool) or not isinstance(intent["size_base_units"], int) or intent["size_base_units"] <= 0: raise ValueError("intent_size")
            if envelope is not None:
                validate_structure(envelope, now=instant)
                verify_signature(envelope, signature_verifier)
                authorize_intent(envelope, intent)
                approval_hash = envelope_hash(envelope)
            self.last_sequence = sequence
        except (ValueError, ApprovalEnvelopeError) as exc:
            reason = str(exc)
        record = {"intent_sequence": intent.get("intent_sequence") if isinstance(intent, dict) else None,
                  "intent_hash": hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest() if isinstance(intent, dict) else None,
                  "decision": "DENY", "reason": reason, "approval_envelope_hash": approval_hash,
                  "signed": False, "submitted": False}
        self.audit.append(record)
        return record
