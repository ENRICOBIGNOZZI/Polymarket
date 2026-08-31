from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v7_clob_v2_auth_test", ROOT / "scripts" / "v7_clob_v2_auth.py")
assert spec and spec.loader
auth = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auth
spec.loader.exec_module(auth)


class ClobV2AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.address = "0x" + "12" * 20
        self.secret_bytes = b"known-test-secret"
        self.credentials = auth.L2Credentials("key-1", base64.b64encode(self.secret_bytes).decode(), "phrase-1")

    def test_l1_typed_data_and_headers_match_clob_auth_contract(self) -> None:
        typed = auth.l1_typed_data(self.address, 123, 0)
        self.assertEqual(typed["domain"], {"name": "ClobAuthDomain", "version": "1", "chainId": 137})
        self.assertEqual(typed["message"]["message"], auth.L1_MESSAGE)
        headers = auth.l1_headers(self.address, "0x" + "ab" * 65, 123, 0)
        self.assertEqual(headers["POLY_NONCE"], "0")
        self.assertEqual(headers["POLY_TIMESTAMP"], "123")

    def test_l2_signature_is_over_exact_wire_body_and_query_is_rejected(self) -> None:
        body = b'{"orderType":"GTC","order":{"salt":7}}'
        request = auth.l2_signed_request(self.credentials, address=self.address, timestamp=123,
                                         method="POST", path="/order", body=body)
        expected = base64.urlsafe_b64encode(hmac.new(
            self.secret_bytes, b"123POST/order" + body, hashlib.sha256).digest()).decode()
        self.assertEqual(request.headers["POLY_SIGNATURE"], expected)
        self.assertIs(request.body, body)
        with self.assertRaisesRegex(auth.ClobAuthError, "query_free"):
            auth.l2_signed_request(self.credentials, address=self.address, timestamp=123,
                                   method="GET", path="/orders?market=x")

    def test_user_subscription_and_credentials_do_not_expose_in_repr(self) -> None:
        frame = auth.user_websocket_subscription(self.credentials, ["market-a"])
        self.assertEqual(frame["type"], "user")
        self.assertEqual(frame["auth"]["apiKey"], "key-1")
        self.assertNotIn("known-test-secret", repr(self.credentials))
        with self.assertRaisesRegex(auth.ClobAuthError, "sorted_unique"):
            auth.user_websocket_subscription(self.credentials, ["z", "a"])


if __name__ == "__main__":
    unittest.main()
