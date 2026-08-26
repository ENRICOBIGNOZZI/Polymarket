from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_trade_recorder_health", ROOT / "scripts" / "validate_trade_recorder_health.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

DIAG_SPEC = importlib.util.spec_from_file_location(
    "diagnose_trade_api_empty_tape", ROOT / "scripts" / "diagnose_trade_api_empty_tape.py"
)
assert DIAG_SPEC and DIAG_SPEC.loader
DIAG = importlib.util.module_from_spec(DIAG_SPEC)
DIAG_SPEC.loader.exec_module(DIAG)


class TradeRecorderHealthTest(unittest.TestCase):
    def fields(self, **updates: int) -> dict[str, int]:
        base = {
            "markets": 400,
            "conditions": 400,
            "requests": 10,
            "fetched": 1812,
            "new_trades": 1812,
            "errors": 0,
            "truncated_batches": 0,
            "last_trade_ts": 1_000_000,
            "seen": 1812,
            "elapsed_ms": 3093,
        }
        base.update(updates)
        return base

    def test_parses_latest_status_line(self) -> None:
        text = "noise\ntrade_recorder markets=400 conditions=400 requests=10 fetched=1812 new_trades=1812 errors=0 truncated_batches=0 last_trade_ts=1000000 seen=1812 elapsed_ms=3093\n"
        self.assertEqual(MODULE.parse_status_line(text), self.fields())

    def test_healthy_current_tape_passes(self) -> None:
        report = MODULE.evaluate(self.fields(), 1_000_300, 1200, 30)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["trade_age_seconds"], 300)

    def test_one_low_rate_transient_error_with_fresh_tape_passes(self) -> None:
        report = MODULE.evaluate(
            self.fields(requests=13, errors=1, fetched=2549, new_trades=2549, seen=2549),
            1_000_056,
            1200,
            30,
        )
        self.assertEqual(report["status"], "healthy")
        self.assertAlmostEqual(report["request_error_rate"], 1 / 13)

    def test_data_api_error_rate_still_fails_closed(self) -> None:
        report = MODULE.evaluate(self.fields(errors=1), 1_000_300, 1200, 30)
        self.assertIn("data_api_request_errors", report["failures"])

    def test_multiple_api_errors_fail_even_at_low_rate(self) -> None:
        report = MODULE.evaluate(self.fields(requests=100, errors=2), 1_000_300, 1200, 30)
        self.assertIn("data_api_request_errors", report["failures"])

    def test_truncated_second_page_fails_closed(self) -> None:
        report = MODULE.evaluate(self.fields(truncated_batches=1), 1_000_300, 1200, 30)
        self.assertIn("trade_batches_truncated", report["failures"])

    def test_missing_condition_mapping_fails_closed(self) -> None:
        report = MODULE.evaluate(self.fields(conditions=399), 1_000_300, 1200, 30)
        self.assertIn("condition_mapping_incomplete", report["failures"])

    def test_empty_or_stale_tape_fails_closed(self) -> None:
        empty = MODULE.evaluate(self.fields(fetched=0, last_trade_ts=0), 1_000_300, 1200, 30)
        self.assertIn("no_public_trades_fetched", empty["failures"])
        self.assertIn("missing_last_trade_timestamp", empty["failures"])
        stale = MODULE.evaluate(self.fields(), 1_001_201, 1200, 30)
        self.assertIn("stale_public_trade_tape", stale["failures"])

    def test_future_timestamp_fails_closed(self) -> None:
        report = MODULE.evaluate(self.fields(last_trade_ts=1_000_100), 1_000_000, 1200, 30)
        self.assertIn("trade_timestamp_in_future", report["failures"])

    def test_recorder_uses_documented_server_window_and_market_filters(self) -> None:
        source = (ROOT / "src" / "trade_recorder.cpp").read_text(encoding="utf-8")
        self.assertIn('"&takerOnly=true&start=" << start << "&end=" << end', source)
        self.assertIn('"&market=" << market_query', source)

    def test_recorder_verifies_server_window_locally_before_dedup(self) -> None:
        source = (ROOT / "src" / "trade_recorder.cpp").read_text(encoding="utf-8")
        window = "if (t.ts < start || t.ts > end) continue;"
        condition = "if (!by_condition.count(t.condition_id)) continue;"
        dedup = "const auto key = trade_key(t);"
        self.assertIn(window, source)
        self.assertLess(source.index(window), source.index(condition))
        self.assertLess(source.index(window), source.index(dedup))

    def test_recorder_caps_data_api_page_and_batch_sizes(self) -> None:
        source = (ROOT / "src" / "trade_recorder.cpp").read_text(encoding="utf-8")
        self.assertIn("constexpr std::size_t page_limit = 1000;", source)
        self.assertIn("constexpr std::size_t max_offset = 10000;", source)
        self.assertIn("std::min<std::size_t>(batch_size_, 20)", source)
        self.assertNotIn("constexpr std::size_t page_limit = 10000;", source)

    def test_recorder_splits_retryable_transport_failures_before_counting_terminal_error(self) -> None:
        source = (ROOT / "src" / "trade_recorder.cpp").read_text(encoding="utf-8")
        self.assertIn("status == 408 || status == 429 || status >= 500", source)
        self.assertIn("return fetch_batch(lo, mid) && fetch_batch(mid, hi);", source)
        split = source.index("if (hi - lo > 1)")
        terminal_error = source.index("++errors;", split)
        self.assertLess(split, terminal_error)

    def test_empty_tape_diagnostic_builds_documented_query_shapes(self) -> None:
        windowed = DIAG.trades_url(conditions=["a", "b"], start=100, end=200)
        self.assertIn("limit=1000", windowed)
        self.assertIn("offset=0", windowed)
        self.assertIn("takerOnly=true", windowed)
        self.assertIn("start=100", windowed)
        self.assertIn("end=200", windowed)
        self.assertIn("market=a,b", windowed)
        unwindowed = DIAG.trades_url(conditions=["a", "b"])
        self.assertNotIn("start=", unwindowed)
        self.assertNotIn("end=", unwindowed)
        self.assertIn("market=a,b", unwindowed)
        global_url = DIAG.trades_url()
        self.assertNotIn("market=", global_url)

    def test_empty_tape_diagnostic_classifies_filter_and_universe_controls(self) -> None:
        empty = {
            "batch_windowed": {"local_window_discovered_matches": 0},
            "batch_unwindowed": {"local_window_discovered_matches": 0},
            "single_windowed": {"local_window_discovered_matches": 0},
            "global_recent": {"local_window_rows": 4, "local_window_discovered_matches": 0},
        }
        self.assertEqual(
            DIAG.classify(empty),
            "global_tape_active_but_sampled_universe_has_no_recent_matches",
        )
        window_interaction = {
            **empty,
            "batch_unwindowed": {"local_window_discovered_matches": 2},
        }
        self.assertEqual(DIAG.classify(window_interaction), "server_window_filter_interaction")
        csv_interaction = {
            **empty,
            "single_windowed": {"local_window_discovered_matches": 1},
        }
        self.assertEqual(DIAG.classify(csv_interaction), "csv_market_filter_interaction")

    def test_live_smoke_runs_bounded_diagnostic_only_after_empty_recorder(self) -> None:
        source = (ROOT / "scripts" / "v6_live_smoke_once.sh").read_text(encoding="utf-8")
        self.assertIn("trade_recorder_latest.log", source)
        self.assertIn("diagnose_trade_api_empty_tape.py", source)
        self.assertIn("trade_api_empty_tape_diagnostic.json", source)
        self.assertIn("fetched=0", source)
        self.assertLess(source.index("polymarket_trade_recorder"), source.index("diagnose_trade_api_empty_tape.py"))


if __name__ == "__main__":
    unittest.main()