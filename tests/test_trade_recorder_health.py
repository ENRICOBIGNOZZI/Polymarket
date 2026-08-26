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

AUDIT_SPEC = importlib.util.spec_from_file_location(
    "lf_v6_trade_tape_health_audit", ROOT / "scripts" / "lf_v6_trade_tape_health_audit.py"
)
assert AUDIT_SPEC and AUDIT_SPEC.loader
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(AUDIT)


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

    def test_unhealthy_tape_plus_new_graph_reservation_is_flagged(self) -> None:
        snapshot = {
            "data_health": {
                "trade_recorder": {
                    "status": "unhealthy",
                    "failures": ["no_public_trades_fetched", "missing_last_trade_timestamp"],
                    "fields": {"fetched": 0, "new_trades": 0, "last_trade_ts": 0, "seen": 0},
                }
            },
            "intents": {"bundles": 1, "strategies": {"GRAPH_RV": 3}},
            "logs": {
                "multileg": [
                    "multileg_tick bundles=1 resting=1 complete=0 aborting=0 closed=0 "
                    "unwound=0 trades_processed=0 tape_cursor=0 reserved=60 cash=5000"
                ]
            },
        }
        report = AUDIT.analyze(snapshot)
        self.assertEqual(report["status"], "FAIL_CLOSED_REQUIRED")
        self.assertTrue(report["unsafe_graph_admission_with_unhealthy_tape"])
        self.assertEqual(report["graph_rv"]["reserved_usd"], 60.0)


if __name__ == "__main__":
    unittest.main()
