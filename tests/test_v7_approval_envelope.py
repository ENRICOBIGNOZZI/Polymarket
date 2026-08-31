from __future__ import annotations

from datetime import datetime, timezone
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_approval_envelope as approval  # noqa: E402
import v7_signer_gateway as gateway  # noqa: E402

NOW = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)


def envelope() -> dict:
    return {"schema_version": 1, "exact_code_sha": "a" * 40, "build_manifest_hash": "c" * 64,
            "config_bundle_hash": "d" * 64, "policy_hash": "b" * 64, "model_hashes": {"maker": "e" * 64},
            "wallet": "private-wallet-ref", "session_key_id": "private-session-ref", "allowed_execution_mode": "MICRO_LIVE",
            "allowed_condition_ids": ["condition"], "allowed_token_ids": ["token"], "allowed_order_types": ["GTC"],
            "require_post_only": True, "maximum_order_base_units": 10, "maximum_gross_exposure_base_units": 10,
            "maximum_event_loss_base_units": 10, "maximum_daily_loss_base_units": 10, "maximum_open_order_count": 1,
            "start_timestamp": "2026-08-31T11:00:00Z", "expiry_timestamp": "2026-08-31T13:00:00Z",
            "approver_identity": "private-approver", "approval_nonce": "nonce-at-least-16", "signature": "opaque-private-signature"}


def intent() -> dict:
    return {"intent_sequence": 1, "exact_code_sha": "a" * 40, "build_manifest_hash": "c" * 64,
            "config_bundle_hash": "d" * 64, "policy_hash": "b" * 64, "execution_mode": "MICRO_LIVE",
            "condition_id": "condition", "token_id": "token", "order_type": "GTC", "post_only": True,
            "size_base_units": 10, "gross_exposure_base_units": 10, "event_loss_base_units": 0,
            "daily_loss_base_units": 0, "open_order_count": 1}


class ApprovalEnvelopeTests(unittest.TestCase):
    def test_signature_verifier_is_mandatory_and_intent_is_allowlisted(self) -> None:
        value = envelope()
        approval.validate_structure(value, now=NOW)
        with self.assertRaisesRegex(approval.ApprovalEnvelopeError, "signature_verifier_required"):
            approval.verify_signature(value, None)
        approval.verify_signature(value, lambda payload, signature, identity: bool(payload) and signature and identity == "private-approver")
        approval.authorize_intent(value, intent())
        candidate = intent(); candidate["token_id"] = "different"
        with self.assertRaisesRegex(approval.ApprovalEnvelopeError, "market_not_allowlisted"):
            approval.authorize_intent(value, candidate)

    def test_valid_approval_cannot_override_checked_in_zero_caps(self) -> None:
        result = gateway.SignerGateway().admit(intent(), envelope=envelope(), now=NOW,
                                               signature_verifier=lambda *_: True)
        self.assertEqual(result["decision"], "DENY")
        self.assertEqual(result["reason"], "CHECKED_IN_LIVE_CAPS_ZERO")
        self.assertRegex(str(result["approval_envelope_hash"]), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
