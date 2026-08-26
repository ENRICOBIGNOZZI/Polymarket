from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V7FastArbLegFreshnessContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.part2 = (ROOT / "src" / "fast_runtime" / "part2.inc").read_text(encoding="utf-8")
        cls.part3 = (ROOT / "src" / "fast_runtime" / "part3.inc").read_text(encoding="utf-8")

    def test_each_l2_leg_has_its_own_exchange_and_receive_clock(self) -> None:
        self.assertIn("book_exchange_ts_ms_", self.part3)
        self.assertIn("book_received_ts_ms_", self.part3)
        self.assertIn("ws_snapshot_ready_", self.part3)
        self.assertIn("mark_l2_clock_locked(token, timestamp, received_ms)", self.part3)
        self.assertIn("kMaxBookAgeMs = 5000", self.part3)
        self.assertIn("kMaxBookSkewMs = 1500", self.part3)

    def test_payload_trigger_clock_is_not_reused_as_multileg_clock(self) -> None:
        self.assertIn("apply_event_locked(value.as_object(), affected, exchange_ms, received_ms)", self.part2)
        self.assertIn("evaluate_market_locked(market_id);", self.part2)
        self.assertNotIn("evaluate_market_locked(market_id, exchange_ms, received_ms)", self.part2)
        self.assertIn("freshness_window_locked", self.part3)
        self.assertIn("FreshnessWindow{min_exchange, max_received}", self.part3)

    def test_delta_cannot_validate_a_rest_seed_without_full_ws_snapshot(self) -> None:
        self.assertIn("if (ws_snapshot_ready_.count(token))", self.part3)
        self.assertIn("ws_snapshot_ready_.insert(token);", self.part3)
        self.assertIn("ws_snapshot_ready_.erase(token);", self.part2)
        self.assertIn("book_exchange_ts_ms_.erase(token);", self.part2)
        self.assertIn("book_received_ts_ms_.erase(token);", self.part2)

    def test_freshness_gate_covers_binary_event_and_relation_evaluations(self) -> None:
        self.assertIn("{market.yes_token, market.no_token}, decision", self.part3)
        self.assertIn("freshness_window_locked(required_tokens, decision)", self.part3)
        self.assertIn("left->second.yes_token, left->second.no_token, right->second.yes_token", self.part3)
        self.assertIn("max_exchange - min_exchange > kMaxBookSkewMs", self.part3)
        self.assertIn("age > kMaxBookAgeMs", self.part3)

    def test_current_opportunity_is_expired_even_without_another_market_message(self) -> None:
        self.assertIn("opportunity_required_tokens_locked(current)", self.part2)
        self.assertIn("freshness_window_locked(required, snapshot_ms, false)", self.part2)
        self.assertIn('current.reject_reason = "stale_or_unsynchronized_leg_book"', self.part2)
        self.assertIn('"current_stale_opportunities"', self.part2)

    def test_rest_resync_does_not_overwrite_a_fresh_ws_l2_lineage(self) -> None:
        self.assertIn("if (fresh_ws) continue;", self.part2)
        self.assertIn("REST is useful for seeding/recovery", self.part2)
        self.assertNotIn("evaluate_all_locked(0, now_ms())", self.part2)

    def test_shadow_safety_boundary_is_unchanged(self) -> None:
        self.assertIn('{"real_order_submission", false}', self.part2)
        combined = self.part2 + "\n" + self.part3
        self.assertNotIn("PRIVATE_KEY", combined)
        self.assertNotIn("--execute", combined)


if __name__ == "__main__":
    unittest.main()
