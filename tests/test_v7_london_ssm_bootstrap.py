from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))
SPEC = importlib.util.spec_from_file_location(
    "v7_london_ssm_bootstrap", ROOT / "ops/v7_london_ssm_bootstrap.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class LondonSsmBootstrapTests(unittest.TestCase):
    def test_remote_bootstrap_is_exact_sha_and_paper_disabled(self) -> None:
        sha = "a" * 40
        command = module.remote_command(sha, "ubuntu")
        self.assertIn(f"SHA={sha}", command)
        self.assertIn('git -C "$APP" fetch --no-tags origin main', command)
        self.assertIn('git -C "$APP" show "$SHA:ops/v7_london_bootstrap.sh"', command)
        self.assertIn("POLYMARKET_EXPECTED_SHA", command)
        self.assertIn("PM_V7_RUN_ROOT", command)
        self.assertIn("! systemctl is-active --quiet polymarket-v7-paper.service", command)
        self.assertNotIn("systemctl enable", command)
        self.assertNotIn("real_order_submission=true", command.lower())

    def test_parse_receipt_is_single_envelope(self) -> None:
        payload = '{"code_sha":"' + "a" * 40 + '","paper_only":true}'
        value = module.parse_receipt("noise\nV7_BOOTSTRAP=" + payload + "\n")
        self.assertEqual(value["code_sha"], "a" * 40)
        with self.assertRaises(ValueError):
            module.parse_receipt("missing")
        with self.assertRaises(ValueError):
            module.parse_receipt("V7_BOOTSTRAP={}\nV7_BOOTSTRAP={}")

    def test_invalid_identity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            module.remote_command("bad", "ubuntu")
        with self.assertRaises(ValueError):
            module.remote_command("a" * 40, "bad user")

    def test_script_uses_dedicated_bootstrap_ssm_comment(self) -> None:
        source = (ROOT / "ops/v7_london_ssm_bootstrap.py").read_text(encoding="utf-8")
        self.assertIn("London exact-SHA PAPER bootstrap", source)
        self.assertIn('"runtime_services_enabled": False', source)
        self.assertIn('"automatic_cutover": False', source)


if __name__ == "__main__":
    unittest.main()
