from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/fast_runtime/part2.inc"


class FastPublishLockScopeTests(unittest.TestCase):
    def test_expensive_publication_work_is_outside_hot_state_lock(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        start = text.index("    void publish(")
        end = text.index("\n    }\n", start) + 6
        block = text[start:end]
        lock_start = block.index("std::scoped_lock lock(mutex_)")
        lock_end = block.index("publish_lock_us =", lock_start)
        hot = block[lock_start:lock_end]
        cold = block[lock_end:]
        self.assertIn("books_snapshot = books_", hot)
        self.assertIn("live_trades_snapshot = live_trades_", hot)
        self.assertNotIn("markets_snapshot", hot)
        self.assertNotIn("fees_snapshot", hot)
        self.assertNotIn("token_to_market_snapshot", hot)
        self.assertNotIn("json::object row", hot)
        self.assertNotIn("shared_books.emplace_back", hot)
        self.assertNotIn("live_flow_rows.emplace_back", hot)
        self.assertIn("shared_books.reserve(books_snapshot.size())", cold)
        self.assertIn("live_flow_rows.reserve(live_trades_snapshot.size())", cold)
        self.assertIn('"publish_snapshot_lock_us"', cold)
        self.assertIn('"publish_total_us"', cold)

    def test_economic_novelty_is_captured_with_snapshot(self) -> None:
        text = SOURCE.read_text(encoding="utf-8")
        publish = text[text.index("    void publish("):]
        lock_end = publish.index("publish_lock_us =")
        novelty = publish.index("last_published_economic_state_[token] = economic_state")
        self.assertGreater(novelty, lock_end)
        self.assertIn("lineage_snapshot[token]", publish)
        self.assertIn("version_snapshot[token]", publish)


if __name__ == "__main__":
    unittest.main()
