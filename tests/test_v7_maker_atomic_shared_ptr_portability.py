from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "include/pm/v7_maker_hft.hpp"


class MakerAtomicSharedPtrPortabilityTest(unittest.TestCase):
    def test_model_store_uses_portable_shared_ptr_atomic_free_functions(self) -> None:
        text = HEADER.read_text(encoding="utf-8")
        bad_member = "std::atomic<" + "std::shared_ptr<const MakerModelSnapshot>> active_;"
        self.assertNotIn(bad_member, text)
        self.assertIn("class AtomicSharedPtrCompat final", text)
        self.assertIn("std::atomic_load_explicit(&value_, order)", text)
        self.assertIn("std::atomic_store_explicit(&value_, std::move(next), order)", text)
        self.assertIn("AtomicSharedPtrCompat<const MakerModelSnapshot> active_;", text)


if __name__ == "__main__":
    unittest.main()
