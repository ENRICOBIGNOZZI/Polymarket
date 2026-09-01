from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("v7_protocol_fuzz_test", ROOT / "scripts/v7_protocol_fuzz.py")
assert spec and spec.loader
fuzz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fuzz)


class ProtocolFuzzTests(unittest.TestCase):
    def test_mutational_fuzz_is_deterministic_and_typed(self) -> None:
        first = fuzz.run(seed=7, iterations=1000)
        second = fuzz.run(seed=7, iterations=1000)
        self.assertEqual(first, second)
        self.assertEqual(first["accepted"] + first["rejected"], 1000)
        self.assertGreater(first["rejected"], 0)

    def test_invalid_invocation_is_rejected(self) -> None:
        with self.assertRaisesRegex(fuzz.ProtocolFuzzError, "iterations"):
            fuzz.run(seed=1, iterations=0)


if __name__ == "__main__":
    unittest.main()
