from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("v7_secret_scan", ROOT / "scripts/v7_secret_scan.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["v7_secret_scan"] = module
    spec.loader.exec_module(module)
    return module


scanner = load()


class SecretScanTests(unittest.TestCase):
    def test_detects_credentials_without_returning_their_value(self) -> None:
        value = "tskey-" + "abcdefghijklmnopqrstuvwxyz123456"
        findings = scanner.scan_text(f"key = {value}\n", location="sample.txt")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "tailscale_auth_key")
        self.assertNotIn(value, findings[0].fingerprint)
        self.assertNotIn(value, findings[0].location)

    def test_detects_pem_and_assigned_api_secret(self) -> None:
        findings = scanner.scan_text(
            "-----BEGIN " + "PRIVATE KEY-----\napi_secret='" + "abcdefghijklmnop" + "'\n",
            location="sample.txt")
        self.assertEqual({row.kind for row in findings}, {"private_key_pem", "assigned_secret"})

    def test_environment_and_documented_placeholders_are_not_findings(self) -> None:
        findings = scanner.scan_text(
            "api_secret = ${STREAMS_API_SECRET}\napi_key = placeholder\n", location="sample.txt")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
