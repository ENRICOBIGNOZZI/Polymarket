#!/usr/bin/env python3
from __future__ import annotations

import gzip
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "external_intelligence", ROOT / "scripts" / "external_intelligence.py"
)
assert SPEC and SPEC.loader
external = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = external
SPEC.loader.exec_module(external)


class ExternalIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(
            (ROOT / "config" / "external_intelligence.json").read_text(encoding="utf-8")
        )
        self.now = 1_800_000_000

    def market(self, question: str = "Will Bitcoin be above $100,000 by December 31 2026?"):
        return external.PmMarket(
            market_id="pm-1",
            condition_id="condition-1",
            event_id="event-1",
            slug="btc-100k",
            question=question,
            description="Bitcoin price at expiry",
            category="crypto",
            end_ts=self.now + 30 * 86400,
            liquidity=10_000.0,
            volume24h=5_000.0,
            bid=0.49,
            ask=0.51,
            mid=0.50,
            yes_token="yes-1",
            no_token="no-1",
            resolved_outcome=None,
        )

    def kalshi(self, title: str = "Bitcoin above $100,000 on December 31 2026"):
        return external.KMarket(
            ticker="KXBTC-100K",
            event_ticker="KXBTC",
            title=title,
            subtitle="",
            rules="Settlement details may contain unrelated numeric identifiers 12345.",
            close_ts=self.now + 30 * 86400,
            updated_ts=self.now,
            bid=0.53,
            ask=0.55,
            mid=0.54,
            spread=0.02,
            volume=1_000.0,
            liquidity=1_000.0,
        )

    def test_config_fails_closed_on_execution_or_live_mutation(self) -> None:
        external.validate_config(self.config)
        for key in (
            "allow_authenticated_execution",
            "allow_direct_champion_mutation",
            "allow_production_signal_write",
        ):
            bad = json.loads(json.dumps(self.config))
            bad[key] = True
            with self.assertRaises(ValueError, msg=key):
                external.validate_config(bad)

    def test_probability_price_supports_dollars_and_legacy_cents(self) -> None:
        raw = {
            "ticker": "KXTEST",
            "title": "Test contract",
            "market_type": "binary",
            "yes_bid": 42,
            "yes_ask": 46,
            "last_price": 44,
            "close_time": self.now + 3600,
        }
        market = external.parse_k_market(raw)
        self.assertIsNotNone(market)
        assert market is not None
        self.assertAlmostEqual(market.bid, 0.42)
        self.assertAlmostEqual(market.ask, 0.46)
        self.assertAlmostEqual(market.mid, 0.44)

    def test_matcher_uses_contract_thresholds_not_unrelated_rule_numbers(self) -> None:
        score = external.score_match(self.market(), self.kalshi(), max_expiry_days=14.0)
        self.assertTrue(score.numeric_match)
        self.assertTrue(score.orientation_match)
        self.assertEqual(score.rejection, "")
        self.assertGreater(score.score, 0.65)

        mismatch = external.score_match(
            self.market(),
            self.kalshi("Bitcoin above $120,000 on December 31 2026"),
            max_expiry_days=14.0,
        )
        self.assertFalse(mismatch.numeric_match)
        self.assertEqual(mismatch.rejection, "critical_number_mismatch")

    def test_compact_gdelt_timestamp_is_point_in_time_parseable(self) -> None:
        timestamp = external.parse_timestamp("20260824T154500Z")
        self.assertGreater(timestamp, 0)
        self.assertEqual(external.iso_utc(timestamp), "2026-08-24T15:45:00+00:00")

    def synthetic_backtest_data(self):
        observations = []
        prices = []
        base = self.now - 100 * 3 * 3600
        market = self.market()
        for index in range(90):
            decision_ts = base + index * 3 * 3600
            sign = 1.0 if index % 2 == 0 else -1.0
            current = 0.50
            future = current + sign * 0.06
            bid, ask = external.synthetic_quote(current, 0.005)
            future_bid, future_ask = external.synthetic_quote(future, 0.005)
            synthetic = external.PmMarket(**{**market.__dict__, "bid": bid, "ask": ask, "mid": current})
            observations.append(
                external.observation_row(
                    observed_ts=decision_ts,
                    market=synthetic,
                    source="kalshi",
                    source_id="KXBTC-100K",
                    feature_name="external_probability",
                    feature_value=sign * 0.08,
                    q_external=current + sign * 0.08,
                    confidence=0.9,
                    mapping_score=0.95,
                    source_event_ts=decision_ts,
                )
            )
            prices.extend(
                [
                    {
                        "schema": external.PRICE_SCHEMA,
                        "observed_ts": decision_ts,
                        "market_id": market.market_id,
                        "bid": bid,
                        "ask": ask,
                        "mid": current,
                    },
                    {
                        "schema": external.PRICE_SCHEMA,
                        "observed_ts": decision_ts + 3600,
                        "market_id": market.market_id,
                        "bid": future_bid,
                        "ask": future_ask,
                        "mid": future,
                    },
                ]
            )
        return observations, prices

    def test_purged_walk_forward_backtest_passes_only_on_elapsed_labels(self) -> None:
        observations, prices = self.synthetic_backtest_data()
        config = json.loads(json.dumps(self.config))
        config["backtest"].update(
            {
                "horizons_seconds": [3600],
                "future_price_tolerance_seconds": 60,
                "min_train_observations": 8,
                "bootstrap_reps": 200,
                "extra_cost_bps": 5.0,
            }
        )
        config["gates"].update(
            {
                "min_oos_predictions": 40,
                "min_trades": 30,
                "max_bootstrap_pvalue": 0.10,
                "min_positive_fold_fraction": 0.75,
            }
        )
        labeled = external.label_observations(observations, prices, 3600, 60)
        self.assertGreater(len(labeled), 80)
        for index in range(1, len(labeled)):
            train = external.purged_training_rows(labeled, index)
            decision = int(labeled[index]["observed_ts"])
            self.assertTrue(all(int(row["future_ts"]) < decision for row in train))

        candidates = external.run_backtests(observations, prices, config)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertTrue(candidate["gate_pass"], candidate["reasons"])
        self.assertGreater(candidate["metrics"]["mse_improvement"], 0.0)
        self.assertGreater(candidate["metrics"]["cost_stress_net_pnl"]["2.0"], 0.0)
        self.assertLessEqual(candidate["raw_pvalue"], 0.10)
        evidence = external.alpha_factory_evidence(candidates)
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence["family"], "external_information")
        self.assertFalse(evidence["integration_evidence_pass"])
        self.assertIn(
            "exact_executable_clob_replay_and_incumbent_ablation_required",
            evidence["integration_reasons"],
        )

    def test_storage_is_deduplicated_bounded_and_reproducible(self) -> None:
        row = {
            "observation_id": "same",
            "observed_ts": self.now,
            "retrieved_ts": self.now,
            "market_id": "pm-1",
            "source": "kalshi",
            "feature_name": "external_probability",
        }
        newer = dict(row, retrieved_ts=self.now + 1, feature_value=0.1)
        merged = external.merge_rows(
            [row],
            [newer, newer],
            identity_fields=("observation_id",),
            max_rows=10,
            min_timestamp=self.now - 1,
        )
        self.assertEqual(merged, [newer])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl.gz"
            external.write_jsonl_gz(path, merged)
            first = path.read_bytes()
            external.write_jsonl_gz(path, merged)
            second = path.read_bytes()
            self.assertEqual(first, second)
            self.assertEqual(external.read_jsonl_gz(path), merged)
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                self.assertEqual(len(handle.readlines()), 1)

    def test_demo_cli_never_touches_production_or_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty_gz = root / "empty.jsonl.gz"
            external.write_jsonl_gz(empty_gz, [])
            empty_state = root / "state.json"
            empty_state.write_text("{}\n", encoding="utf-8")
            report = root / "report.json"
            command = [
                "python3",
                str(ROOT / "scripts" / "external_intelligence.py"),
                "--config",
                str(ROOT / "config" / "external_intelligence.json"),
                "--observations-in",
                str(empty_gz),
                "--prices-in",
                str(empty_gz),
                "--state-in",
                str(empty_state),
                "--observations-out",
                str(root / "observations.jsonl.gz"),
                "--prices-out",
                str(root / "prices.jsonl.gz"),
                "--state-out",
                str(root / "state-out.json"),
                "--signals-out",
                str(root / "signals.jsonl"),
                "--report-json",
                str(report),
                "--report-markdown",
                str(root / "report.md"),
                "--mode",
                "demo",
                "--now",
                str(self.now),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(payload["paper_only"])
            self.assertEqual(payload["submitted_orders"], 0)
            self.assertFalse(payload["authenticated_execution"])
            self.assertFalse(payload["direct_champion_mutation"])
            self.assertFalse(payload["production_signal_write"])
            self.assertGreater(payload["collection"]["kalshi_matches"], 0)


if __name__ == "__main__":
    unittest.main()
