from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class V7FastArbLegFreshnessContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.part2 = (ROOT / "src" / "fast_runtime" / "part2.inc").read_text(encoding="utf-8")
        cls.part3 = (ROOT / "src" / "fast_runtime" / "part3.inc").read_text(encoding="utf-8")
        cls.fast_ws = (ROOT / "src" / "fast_ws.cpp").read_text(encoding="utf-8")

    def test_each_l2_leg_has_its_own_exchange_and_receive_clock(self) -> None:
        self.assertIn("book_exchange_ts_ms_", self.part3)
        self.assertIn("book_received_ts_ms_", self.part3)
        self.assertIn("ws_snapshot_ready_", self.part3)
        self.assertIn("rest_snapshot_ready_", self.part3)
        self.assertIn("mark_l2_clock_locked(token, timestamp, received_ms)", self.part3)
        self.assertIn("kMaxBookAgeMs = 5000", self.part3)
        self.assertIn("kMaxBookSkewMs = 1500", self.part3)

    def test_payload_trigger_clock_is_not_reused_as_multileg_clock(self) -> None:
        self.assertIn("apply_event_locked(value.as_object(), affected, exchange_ms, received_ms)", self.part2)
        self.assertIn("evaluate_market_locked(market_id);", self.part2)
        self.assertNotIn("evaluate_market_locked(market_id, exchange_ms, received_ms)", self.part2)
        self.assertIn("freshness_window_locked", self.part3)
        self.assertIn("FreshnessWindow{min_exchange, max_received}", self.part3)

    def test_timestamped_rest_book_is_point_in_time_provenance_only(self) -> None:
        self.assertIn('cfg_.clob_url + "/books"', self.part3)
        self.assertIn('get_integer(object, "timestamp", 0)', self.part3)
        self.assertIn('snapshot.hash = get_text(object, "hash")', self.part3)
        self.assertIn("rest_snapshot_ready_.insert(token);", self.part2)
        self.assertIn("!snapshot.hash.empty()", self.part2)
        self.assertIn("if (!continuous_ws && !point_in_time_rest)", self.part3)

    def test_continuous_ws_state_is_not_aged_by_last_token_update(self) -> None:
        self.assertIn("const bool continuous_ws = ws_snapshot_ready_.count(token) > 0;", self.part3)
        self.assertIn("if (!continuous_ws) {", self.part3)
        self.assertIn("last-update age/skew are diagnostics", self.part3)
        self.assertIn("A transport failure/reconnect clears this lineage", self.part3)

    def test_rest_point_in_time_state_keeps_strict_age_and_skew(self) -> None:
        self.assertIn("const auto age = decision_ms - exchange->second;", self.part3)
        self.assertIn("age > kMaxBookAgeMs || age < -kMaxBookSkewMs", self.part3)
        self.assertIn("max_rest_exchange - min_rest_exchange > kMaxBookSkewMs", self.part3)
        self.assertIn("max_rest_received - min_rest_received > kMaxBookSkewMs", self.part3)

    def test_delta_cannot_upgrade_a_rest_snapshot_into_ws_lineage(self) -> None:
        self.assertIn("if (!ws_snapshot_ready_.count(token) || timestamp <= 0) continue;", self.part3)
        self.assertIn("rest_snapshot_ready_.erase(token);", self.part3)
        self.assertIn("ws_snapshot_ready_.insert(token);", self.part3)
        self.assertIn("REST image is valid point-in-time", self.part3)

    def test_out_of_order_ws_update_cannot_regress_a_newer_snapshot(self) -> None:
        self.assertGreaterEqual(self.part3.count("previous->second > timestamp"), 2)
        self.assertIn("timestamp > 0 && previous != book_exchange_ts_ms_.end()", self.part3)

    def test_binary_complete_set_requires_both_executable_outcomes(self) -> None:
        self.assertIn("{market.yes_token, market.no_token}, decision", self.part3)

    def test_negrisk_complete_set_requires_only_executable_yes_legs(self) -> None:
        self.assertIn("complete_set_tokens.push_back(member->yes_token)", self.part3)
        self.assertNotIn("complete_set_tokens.push_back(member->no_token)", self.part3)
        self.assertIn("freshness_window_locked(complete_set_tokens, decision)", self.part3)

    def test_negrisk_conversion_uses_source_no_plus_target_yes_legs(self) -> None:
        self.assertIn("conversion_tokens.push_back(source->no_token)", self.part3)
        self.assertIn("conversion_tokens.push_back(target->yes_token)", self.part3)
        self.assertIn("freshness_window_locked(conversion_tokens, decision)", self.part3)

    def test_relation_freshness_matches_the_actual_evaluated_basket(self) -> None:
        self.assertIn("{left->second.no_token, right->second.yes_token}, decision", self.part3)
        self.assertIn("{left->second.no_token, right->second.no_token}, decision", self.part3)
        self.assertIn("{left->second.yes_token, right->second.yes_token}, decision", self.part3)
        self.assertNotIn(
            "{left->second.yes_token, left->second.no_token, right->second.yes_token,\n             right->second.no_token}",
            self.part3,
        )

    def test_publish_revalidation_uses_only_actual_opportunity_leg_tokens(self) -> None:
        start = self.part3.index("std::vector<std::string> opportunity_required_tokens_locked")
        end = self.part3.index("void apply_event_locked", start)
        helper = self.part3[start:end]
        self.assertIn("required.push_back(leg.token_id)", helper)
        self.assertNotIn("market->second.yes_token", helper)
        self.assertNotIn("market->second.no_token", helper)

    def test_current_opportunity_is_expired_even_without_another_market_message(self) -> None:
        self.assertIn("opportunity_required_tokens_locked(current)", self.part2)
        self.assertIn("freshness_window_locked(required, snapshot_ms, false)", self.part2)
        self.assertIn('current.reject_reason = "stale_or_unsynchronized_leg_book"', self.part2)
        self.assertIn('"current_stale_opportunities"', self.part2)

    def test_rest_resync_never_downgrades_uninterrupted_ws_lineage(self) -> None:
        self.assertIn("const bool fresh_ws = ws_snapshot_ready_.count(token) > 0;", self.part2)
        self.assertIn("if (fresh_ws) continue;", self.part2)
        self.assertNotIn("observed_ms - received->second <= kMaxBookAgeMs", self.part2)
        self.assertIn("still-continuous WS lineage", self.part2)
        self.assertNotIn("evaluate_all_locked(0, now_ms())", self.part2)

    def test_reconnect_invalidates_ws_lineage_but_not_independent_rest_snapshot(self) -> None:
        self.assertIn("shard * options_.shard_size", self.part2)
        self.assertIn("ws_snapshot_ready_.erase(token);", self.part2)
        self.assertIn("if (!rest_snapshot_ready_.count(token))", self.part2)
        self.assertIn("websocket closed; reconnecting and invalidating L2 lineage", self.fast_ws)
        self.assertIn("report(shard_index", self.fast_ws)

    def test_silent_ws_failure_is_bounded_before_quiet_lineage_is_trusted(self) -> None:
        self.assertIn("websocket::stream_base::timeout timeouts", self.fast_ws)
        self.assertIn("std::chrono::seconds(60)", self.fast_ws)
        self.assertIn('ws.write(asio::buffer(std::string_view{"PING"}), error)', self.fast_ws)
        self.assertIn("websocket closed; reconnecting and invalidating L2 lineage", self.fast_ws)

    def test_status_separates_ws_and_rest_provenance_coverage(self) -> None:
        self.assertIn('"ws_snapshot_ready_tokens"', self.part2)
        self.assertIn('"rest_point_in_time_ready_tokens"', self.part2)
        self.assertIn('"freshness_ready_tokens"', self.part2)

    def test_shadow_safety_boundary_is_unchanged(self) -> None:
        self.assertIn('{"real_order_submission", false}', self.part2)
        combined = self.part2 + "\n" + self.part3 + "\n" + self.fast_ws
        self.assertNotIn("PRIVATE_KEY", combined)
        self.assertNotIn("--execute", combined)


if __name__ == "__main__":
    unittest.main()
