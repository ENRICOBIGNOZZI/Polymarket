import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_signer_gateway as gateway  # noqa: E402


def valid() -> dict:
    return {"intent_sequence": 1, "exact_code_sha": "a" * 40, "policy_hash": "b" * 64,
            "execution_mode": "MICRO_LIVE", "condition_id": "condition", "token_id": "token",
            "order_type": "GTC", "post_only": True, "size_base_units": 10}


class SignerGatewayTests(unittest.TestCase):
    def test_even_matching_intent_is_not_signed_when_checked_in_live_caps_are_zero(self) -> None:
        result = gateway.SignerGateway().admit(valid())
        self.assertEqual(result["decision"], "DENY")
        self.assertFalse(result["signed"])
        self.assertEqual(result["reason"], "CHECKED_IN_LIVE_CAPS_ZERO")

    def test_bad_intent_is_rejected_before_private_signer_boundary(self) -> None:
        intent = valid(); intent["condition_id"] = ""
        self.assertIn("intent_market", gateway.SignerGateway().admit(intent)["reason"])


if __name__ == "__main__":
    unittest.main()
