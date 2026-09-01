from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("v7_entropy_secret_scan", ROOT / "scripts/v7_entropy_secret_scan.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scanner = load()


class EntropySecretScanTests(unittest.TestCase):
    def test_high_entropy_token_is_redacted(self) -> None:
        token = "aB9_" + "Q7x-" + "mN2p" + "L8vR" + "k3Yz" + "T5wC" + "d6Hs" + "J1qE"
        findings = scanner.scan_text(f"session_token={token}\n", location="sample.txt")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "high_entropy_token")
        self.assertNotIn(token, findings[0].fingerprint)
        self.assertNotIn(token, findings[0].location)

    def test_hex_digest_is_not_an_entropy_secret(self) -> None:
        self.assertEqual(scanner.scan_text("digest=" + "a" * 64 + "\n", location="sample.txt"), [])


if __name__ == "__main__":
    unittest.main()
