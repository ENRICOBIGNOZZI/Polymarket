from __future__ import annotations

import importlib.util
import json
import sys
import unittest
import base64
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


wire = load("v7_clob_v2_order_wire")
auth = load("v7_clob_v2_auth")
ADDRESS = "0x" + "12" * 20
TOKEN_ID = "107505882767731489358349912513945399560393482969656700824895970500493757150417"
SIGNATURE = "0x" + "ab" * 65


class ClobV2OrderWireTests(unittest.TestCase):
    def typed(self, **changes):
        values = {"maker": ADDRESS, "signer": ADDRESS, "token_id": TOKEN_ID,
                  "maker_amount": "5200000", "taker_amount": "10000000", "side": "BUY",
                  "signature_type": 1, "timestamp_ms": 1782753357257, "salt": 479249096354,
                  "neg_risk": False}
        values.update(changes)
        return wire.order_typed_data(**values)

    def test_eip712_order_and_exact_submission_bytes_match_v2_contract(self) -> None:
        typed = self.typed()
        self.assertEqual(typed["domain"], {"name": "Polymarket CTF Exchange", "version": "2",
                                            "chainId": 137, "verifyingContract": wire.STANDARD_EXCHANGE})
        self.assertEqual(typed["primaryType"], "Order")
        self.assertEqual(typed["message"]["side"], 0)
        self.assertEqual(typed["message"]["makerAmount"], "5200000")
        raw = wire.order_submission_bytes(typed, signature=SIGNATURE, owner="api-key", post_only=True)
        self.assertEqual(raw, wire.order_submission_bytes(typed, signature=SIGNATURE, owner="api-key", post_only=True))
        body = json.loads(raw)
        self.assertEqual(body["order"]["side"], "BUY")
        self.assertEqual(body["order"]["salt"], 479249096354)
        self.assertEqual(body["orderType"], "GTC")
        self.assertTrue(body["postOnly"])

    def test_negative_risk_and_order_constraints_are_fail_closed(self) -> None:
        self.assertEqual(self.typed(neg_risk=True)["domain"]["verifyingContract"], wire.NEG_RISK_EXCHANGE)
        with self.assertRaisesRegex(wire.ClobOrderWireError, "gtc_must_be_zero"):
            wire.order_submission_bytes(self.typed(), signature=SIGNATURE, owner="api", expiration=1)
        with self.assertRaisesRegex(wire.ClobOrderWireError, "not_json_safe"):
            self.typed(salt=wire.MAX_SAFE_JSON_INTEGER + 1)
        with self.assertRaisesRegex(wire.ClobOrderWireError, "invalid_hex"):
            wire.order_submission_bytes(self.typed(), signature="not-a-signature", owner="api")

    def test_deposit_wallet_uses_erc7739_typed_data_wrapper(self) -> None:
        deposit = self.typed(signature_type=3, wallet_kind="deposit")
        self.assertEqual(deposit["primaryType"], "TypedDataSign")
        self.assertEqual(deposit["message"]["name"], "DepositWallet")
        self.assertEqual(deposit["message"]["verifyingContract"], ADDRESS)
        self.assertEqual(deposit["message"]["contents"]["signatureType"], 3)
        body = json.loads(wire.order_submission_bytes(deposit, signature=SIGNATURE, owner="api"))
        self.assertEqual(body["order"]["signatureType"], 3)
        with self.assertRaisesRegex(wire.ClobOrderWireError, "deposit_required"):
            self.typed(signature_type=3)
        with self.assertRaisesRegex(wire.ClobOrderWireError, "deposit_requires"):
            self.typed(signature_type=1, wallet_kind="deposit")

    def test_l2_signature_covers_the_exact_serialized_order_body(self) -> None:
        credentials = auth.L2Credentials("api-key", base64.b64encode(b"test-secret").decode(), "phrase")
        request = wire.l2_signed_order_request(
            auth, credentials, address=ADDRESS, timestamp=123, typed_data=self.typed(),
            signature=SIGNATURE, owner="api-key", post_only=True)
        expected = wire.order_submission_bytes(self.typed(), signature=SIGNATURE,
                                               owner="api-key", post_only=True)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, "/order")
        self.assertEqual(request.body, expected)
        with self.assertRaisesRegex(wire.ClobOrderWireError, "l2_signer_missing"):
            wire.l2_signed_order_request(object(), credentials, address=ADDRESS, timestamp=123,
                                         typed_data=self.typed(), signature=SIGNATURE, owner="api-key")


if __name__ == "__main__":
    unittest.main()
