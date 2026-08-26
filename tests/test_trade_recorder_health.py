from __future__ import annotations

import importlib.util
import json
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
    "alpha_factory_data_health_audit", ROOT / "scripts" / "alpha_factory_data_health_audit.py"
)
assert AUDIT_SPEC and AUDIT_SPEC.loader
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(AUDIT)

ALPHA_SPEC = importlib.util.spec_from_file_location(
    "alpha_factory", ROOT / "scripts" / "alpha_factory.py"
)
assert ALPHA_SPEC and ALPHA_SPEC.loader
ALPHA = importlib.util.module_from_spec(ALPHA_SPEC)
ALPHA_SPEC.loader.exec_module(ALPHA)


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

    def test_alpha_factory_audit_allows_healthy_live_execution_evidence(self) -> None:
        live = {
            "generated_ts": 1_800_000_000,
            "data_health": {
                "trade_recorder": {"status": "healthy", "failures": []}
            },
            "walk_forward": {"oos": {"trades": 0}},
        }
        result = AUDIT.evaluate(live)
        self.assertTrue(result["execution_evidence_usable"])
        self.assertEqual(result["decision"], "ALLOW_LIVE_EXECUTION_INFERENCE")

    def test_alpha_factory_current_contract_misclassifies_fresh_unhealthy_zero_execution(self) -> None:
        now = 1_800_000_000
        live = {
            "schema": "polymarket_public_live_smoke_v2",
            "generated_ts": now - 60,
            "git_sha": "fixture",
            "data_health": {
                "trade_recorder": {
                    "status": "unhealthy",
                    "failures": ["no_public_trades_fetched", "missing_last_trade_timestamp"],
                }
            },
            "candidates": {"b1": [], "b2": [], "b3_rewards": []},
            "walk_forward": {
                "input_trades": 0,
                "active_folds": 0,
                "positive_active_folds": 0,
                "bootstrap_one_sided_pvalue": 1.0,
                "gate_failures": [],
                "oos": {"trades": 0},
            },
        }
        config = json.loads((ROOT / "config" / "alpha_factory.json").read_text(encoding="utf-8"))
        champion = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        report, _ = ALPHA.build_report(config, champion, live, {}, [], {}, now)
        experiment_ids = {
            row["experiment_id"] for row in report.get("next_experiments", [])
        }
        self.assertIn("execution_fillability_frontier", experiment_ids)
        self.assertNotEqual(report["status"], "DEGRADED_DATA_HEALTH")

        audit = AUDIT.evaluate(live, report)
        self.assertFalse(audit["execution_evidence_usable"])
        self.assertEqual(audit["decision"], "BLOCK_LIVE_EXECUTION_INFERENCE")
        self.assertIn("execution_fillability_frontier", audit["contaminated_live_execution_experiments"])
        self.assertIn("no_public_trades_fetched", audit["trade_recorder_failures"])


if __name__ == "__main__":
    unittest.main()
