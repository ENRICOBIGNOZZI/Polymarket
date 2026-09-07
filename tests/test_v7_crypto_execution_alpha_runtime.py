from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.v7_crypto_execution_alpha_runtime import (
    CANCEL_REPORT_SCHEMA,
    CANCEL_RULE_SHA,
    CANCEL_SIGNAL_SCHEMA,
    cancel_evidence,
    process_cut,
)
from scripts.v7_opportunity import OpportunityEnvelope


class CryptoExecutionAlphaRuntimeTests(unittest.TestCase):
    def fixture(self, root: Path, *, mature_maker: bool = False, fair=(0.54, 0.56, 0.58)) -> None:
        for path in (
            root / "control", root / "external_fair", root / "micro_maker",
            root / "opportunities" / "inbox",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (root / "control" / "runtime_status.json").write_text(json.dumps({
            "schema": "polymarket_v7_runtime_status_v3",
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "model_sha": "a" * 40,
            "config_hash": "b" * 40, "policy_hash": "c" * 40,
            "run_id": "run-fixture",
        }))
        (root / "external_fair" / "status.json").write_text(json.dumps({
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False,
            "fair": {"valid": True, "lower": fair[0], "yes": fair[1], "upper": fair[2], "tte_seconds": 60.0},
            "market": {
                "market_id": "m1", "event_id": "e1", "yes_token": "yes1", "no_token": "no1",
                "fee_schedule": {"rate": 0.0, "exponent": 1.0, "takerOnly": True},
            },
            "contract": {"verified": True},
            "settlement_reference": {"valid": True},
            "model": {"mature": mature_maker},
            "external": {"realized_vol_fast": 0.00002},
        }))
        (root / "external_fair" / "paper_router_status.json").write_text(json.dumps({
            "live_market": {
                "market_id": "m1", "valid": True, "snapshot_id": "snap1",
                "execution_alpha_books": {
                    "YES": {"token_id": "yes1", "best_bid": 0.50, "best_ask": 0.52,
                            "best_bid_size": 100.0, "best_ask_size": 100.0,
                            "exchange_ts_ms": 1, "receive_ts_ms": 1, "snapshot_id": "ys"},
                    "NO": {"token_id": "no1", "best_bid": 0.48, "best_ask": 0.50,
                           "best_bid_size": 100.0, "best_ask_size": 100.0,
                           "exchange_ts_ms": 1, "receive_ts_ms": 1, "snapshot_id": "ns"},
                },
            },
            "paper_exploration_account": {
                "complete": True, "orders_submitted": 100, "fills": 100, "terminal_nonfills": 0,
            },
        }))
        (root / "control" / "crypto_settlement_engine_snapshot.json").write_text(json.dumps({
            "crypto_context": {
                "asset": "BTC", "horizon": "M5", "contract_family": "BTC_USD_UPDOWN_5M",
                "settlement_semantic_hash": "d" * 64,
            },
            "maker_execution": {
                "valid": mature_maker,
                "execution_model_mature": mature_maker,
                "markout_model_mature": mature_maker,
                "reach_probability_lower": 0.8,
                "fill_given_reach_probability_lower": 0.8,
                "adverse_markout_upper_per_share": 0.002,
            },
            "latency": {"valid": True, "profile_version": 7},
        }))
        (root / "micro_maker" / "execution_model.json").write_text(json.dumps({
            "economically_mature": mature_maker,
            "groups": {"GLOBAL": {"fill_probability": 0.8, "adverse_markout_per_share": 0.002}},
        }))

    @staticmethod
    def policy(path: Path, *, risk: float = 0.03) -> None:
        path.write_text(json.dumps({
            "taker": {
                "base_execution_risk_per_share": risk,
                "tte_bucket_policy": [{
                    "minimum_seconds": 0.0, "maximum_seconds": 300.0,
                    "execution_risk_per_share": risk,
                }],
            },
        }))

    def test_missing_shared_book_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); self.fixture(root)
            policy = root / "policy.json"; self.policy(policy)
            router = json.loads((root / "external_fair" / "paper_router_status.json").read_text())
            router["live_market"].pop("execution_alpha_books")
            (root / "external_fair" / "paper_router_status.json").write_text(json.dumps(router))
            status = process_cut(
                root, external_policy_path=policy, cancel_report_path=None,
                cancel_signal_path=None, comparison_size_shares=5.0,
            )
            self.assertEqual(status["state"], "FAIL_CLOSED")
            self.assertIn("CAUSAL_COMPLEMENT_BOOK_SNAPSHOT_MISSING", status["blockers"])
            self.assertEqual(list((root / "opportunities" / "inbox").glob("*.json")), [])

    def test_immature_maker_is_diagnostic_only_and_never_publishes_make(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); self.fixture(root, mature_maker=False, fair=(0.51, 0.80, 0.90))
            policy = root / "policy.json"; self.policy(policy, risk=0.02)
            status = process_cut(
                root, external_policy_path=policy, cancel_report_path=None,
                cancel_signal_path=None, comparison_size_shares=5.0,
            )
            self.assertEqual(status["state"], "RUNNING")
            self.assertFalse(status["make_opportunity_published"])
            self.assertTrue(status["maker_probe_recommended"])
            self.assertEqual(list((root / "opportunities" / "inbox").glob("*.json")), [])
            recommendations = (root / "crypto_execution_alpha" / "maker_probe_recommendations.jsonl").read_text().splitlines()
            self.assertEqual(len(recommendations), 1)

    def test_mature_make_proposal_enters_existing_coordinator_contract_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); self.fixture(root, mature_maker=True)
            policy = root / "policy.json"; self.policy(policy, risk=0.03)
            first = process_cut(
                root, external_policy_path=policy, cancel_report_path=None,
                cancel_signal_path=None, comparison_size_shares=5.0,
            )
            self.assertTrue(first["make_opportunity_published"])
            files = list((root / "opportunities" / "inbox").glob("*.json"))
            self.assertEqual(len(files), 1)
            envelope = json.loads(files[0].read_text())
            parsed = OpportunityEnvelope.parse(envelope)
            self.assertEqual((parsed.engine_id, parsed.action), ("CRYPTO_SETTLEMENT_ENGINE", "MAKE"))
            self.assertEqual(envelope["component_provenance"], ["crypto_settlement_fair", "professional_maker"])
            second = process_cut(
                root, external_policy_path=policy, cancel_report_path=None,
                cancel_signal_path=None, comparison_size_shares=5.0,
            )
            self.assertFalse(second["make_opportunity_published"])
            self.assertEqual(len(list((root / "opportunities" / "inbox").glob("*.json"))), 1)

    def test_cancel_requires_both_frozen_forward_pass_and_exact_live_signal(self) -> None:
        report = {
            "schema": CANCEL_REPORT_SCHEMA, "state": "PASS", "rule_sha256": CANCEL_RULE_SHA,
            "market_count": 30, "avoidable_fill_events": 60, "episode_count": 300,
            "stress_3x_queue_200ms_cancel_improvement_per_share": 0.01,
            "bootstrap95_market_cluster_500ms_improvement": [0.005, 0.03],
        }
        evidence, reasons = cancel_evidence(report, {})
        self.assertTrue(evidence.mature)
        self.assertFalse(evidence.signal_active)
        self.assertIn("CANONICAL_EXTERNAL_CANCEL_SIGNAL_INACTIVE_OR_MISSING", reasons)
        signal = {
            "schema": CANCEL_SIGNAL_SCHEMA, "rule_sha256": CANCEL_RULE_SHA,
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False,
            "execution_authority": "SIGNAL_ONLY_ZERO_AUTHORITY",
            "receive_time_causal": True, "shock_source": "BINANCE_SPOT_TRADES",
            "shock_window_ms": 100, "minimum_absolute_log_return_bp": 0.3,
            "confirmation_source": "COINBASE_SPOT_TOP_OF_BOOK",
            "confirmation": "NON_OPPOSING", "trigger_cooldown_ms": 250,
            "evaluation_tick_ms": 25, "history_valid": True,
            "threshold_crossed": True, "confirmation_non_opposing": True,
            "cooldown_blocked": False, "direction": "UP",
            "stale_sides": ["YES_SELL", "NO_BUY"],
            "trigger_monotonic_ns": 1_000_000_000,
            "evaluated_monotonic_ns": 1_000_000_000,
            "active": True, "mandatory_risk_cancel": True,
            "active_quote_size_shares": 5.0, "cancel_cost": 0.001,
        }
        active, reasons = cancel_evidence(report, signal)
        self.assertTrue(active.mature and active.signal_active and active.mandatory_risk_cancel)
        self.assertGreater(active.avoidable_fill_probability_lower, 0.0)
        self.assertEqual(reasons, [])
        drifted = dict(signal)
        drifted["shock_window_ms"] = 250
        rejected, reasons = cancel_evidence(report, drifted)
        self.assertFalse(rejected.signal_active)
        self.assertIn("CANONICAL_EXTERNAL_CANCEL_SIGNAL_INACTIVE_OR_MISSING", reasons)


if __name__ == "__main__":
    unittest.main()
