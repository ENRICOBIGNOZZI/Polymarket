#!/usr/bin/env python3
from __future__ import annotations

import gzip
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("external_intelligence", ROOT / "scripts" / "external_intelligence.py")
assert SPEC and SPEC.loader
external = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = external
SPEC.loader.exec_module(external)


class ExternalIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "config" / "external_intelligence.json").read_text(encoding="utf-8"))
        self.now = 1_800_000_000

    def market(self, question: str = "Will Bitcoin be above $100,000 by December 31 2026?"):
        return external.PmMarket(
            market_id="pm-1", condition_id="condition-1", event_id="event-1",
            question=question, description="Bitcoin price at expiry", category="crypto",
            end_ts=self.now + 30 * 86400, liquidity=10000.0, volume24h=5000.0,
            bid=0.49, ask=0.51, mid=0.50, yes_token="yes-1", no_token="no-1",
            resolved_outcome=None,
        )

    def kalshi(self, title: str = "Bitcoin above $100,000 on December 31 2026"):
        return external.KMarket(
            ticker="KXBTC-100K", event_ticker="KXBTC", title=title, subtitle="",
            rules="Settlement details contain unrelated numeric identifier 12345.",
            close_ts=self.now + 30 * 86400, updated_ts=self.now,
            bid=0.53, ask=0.55, mid=0.54, spread=0.02, volume=1000.0, liquidity=1000.0,
        )

    def test_config_fails_closed(self) -> None:
        external.validate_config(self.config)
        for key in ("allow_authenticated_execution", "allow_direct_champion_mutation", "allow_production_signal_write"):
            bad = json.loads(json.dumps(self.config))
            bad[key] = True
            with self.assertRaises(ValueError):
                external.validate_config(bad)

    def test_kalshi_legacy_cent_fields_are_normalized(self) -> None:
        market = external.parse_k_market({
            "ticker": "KXTEST", "title": "Test", "market_type": "binary",
            "yes_bid": 42, "yes_ask": 46, "last_price": 44,
            "close_time": self.now + 3600,
        })
        self.assertIsNotNone(market)
        assert market is not None
        self.assertAlmostEqual(market.bid, 0.42)
        self.assertAlmostEqual(market.ask, 0.46)
        self.assertAlmostEqual(market.mid, 0.44)

    def test_matcher_ignores_unrelated_rule_numbers_but_rejects_strike_mismatch(self) -> None:
        score, numbers, orient, _, rejection = external.score_pair(self.market(), self.kalshi(), 14.0)
        self.assertTrue(numbers)
        self.assertTrue(orient)
        self.assertEqual(rejection, "")
        self.assertGreater(score, 0.65)
        _, numbers, _, _, rejection = external.score_pair(
            self.market(), self.kalshi("Bitcoin above $120,000 on December 31 2026"), 14.0
        )
        self.assertFalse(numbers)
        self.assertEqual(rejection, "critical_number_mismatch")

    def test_kalshi_prefilter_rejects_cross_asset(self) -> None:
        ethereum = self.kalshi("Ethereum above $100,000 on December 31 2026")
        self.assertFalse(external.kalshi_candidate_compatible(self.market(), ethereum))
        self.assertIsNone(external.match_kalshi(self.market(), [ethereum], self.config, self.now))

    def test_crypto_threshold_probability_is_direct_and_bounded(self) -> None:
        features = {
            "spot": 90_000.0,
            "return_5m": 0.001,
            "return_1h": 0.003,
            "return_24h": 0.01,
            "realized_vol_24h": 0.04,
        }
        reach = external.crypto_threshold_probability(
            self.market("Will Bitcoin reach $100,000 in August?"), "BTC", features, self.now, self.config
        )
        self.assertIsNotNone(reach)
        assert reach is not None
        q_reach, confidence, metadata = reach
        self.assertGreater(q_reach, 0.05)
        self.assertLess(q_reach, 0.997)
        self.assertGreaterEqual(confidence, 0.35)
        self.assertEqual(metadata["event_type"], "upper_barrier")

        dip = external.crypto_threshold_probability(
            self.market("Will Bitcoin dip to $75,000 in August?"), "BTC", features, self.now, self.config
        )
        self.assertIsNotNone(dip)
        assert dip is not None
        self.assertGreater(dip[0], 0.01)
        self.assertEqual(dip[2]["event_type"], "lower_barrier")

        crossed = dict(features, spot=105_000.0)
        crossed_estimate = external.crypto_threshold_probability(
            self.market("Will Bitcoin reach $100,000 in August?"), "BTC", crossed, self.now, self.config
        )
        assert crossed_estimate is not None
        self.assertGreater(crossed_estimate[0], 0.99)

    def test_crypto_threshold_probability_abstains_on_ranges(self) -> None:
        features = {"spot": 100_000.0, "return_24h": 0.0, "realized_vol_24h": 0.04}
        market = self.market("Will Bitcoin trade between $90,000 and $110,000 in August?")
        self.assertIsNone(external.crypto_threshold_probability(market, "BTC", features, self.now, self.config))

    def test_crypto_features_and_collector_emit_direct_probability(self) -> None:
        rows = []
        for index in range(289):
            close = 90_000.0 * math.exp(0.00005 * index)
            close_ts_ms = (self.now - (288 - index) * 300) * 1000
            rows.append([0, 0, 0, 0, str(close), 0, close_ts_ms])
        features, source_ts = external.crypto_features(rows)
        self.assertGreater(features["spot"], 90_000.0)
        original = external.fetch_binance_klines
        external.fetch_binance_klines = lambda *args, **kwargs: rows
        try:
            observations, health, errors = external.collect_binance(
                [self.market("Will Bitcoin reach $100,000 in August?")], self.config, self.now
            )
        finally:
            external.fetch_binance_klines = original
        self.assertFalse(errors)
        self.assertEqual(health["BTC"]["status"], "ok")
        direct = [row for row in observations if row["feature_name"] == "external_probability"]
        self.assertEqual(len(direct), 1)
        self.assertIsNotNone(direct[0]["q_external"])
        self.assertEqual(direct[0]["source_event_ts"], source_ts)

    def test_pm_history_uses_supported_interval_without_oversized_range(self) -> None:
        calls = []
        original = external.request_json
        external.request_json = lambda url, **kwargs: calls.append(url) or {
            "history": [{"t": self.now - 3600, "p": 0.5}]
        }
        try:
            rows = external.fetch_pm_history("token", self.now - 14 * 86400, self.now)
        finally:
            external.request_json = original
        self.assertEqual(len(rows), 1)
        self.assertIn("interval=1m", calls[0])
        self.assertNotIn("startTs", calls[0])
        self.assertNotIn("endTs", calls[0])

    def test_gdelt_compact_timestamp(self) -> None:
        timestamp = external.parse_timestamp("20260824T154500Z")
        self.assertEqual(external.iso_utc(timestamp), "2026-08-24T15:45:00+00:00")

    def synthetic_backtest_data(self):
        observations, prices = [], []
        market = self.market()
        base = self.now - 100 * 3 * 3600
        for index in range(90):
            decision_ts = base + index * 3 * 3600
            sign = 1.0 if index % 2 == 0 else -1.0
            current = 0.50
            future = current + sign * 0.06
            bid, ask = external.synthetic_quote(current, 0.005)
            future_bid, future_ask = external.synthetic_quote(future, 0.005)
            synthetic = external.PmMarket(**{**external.asdict(market), "bid": bid, "ask": ask, "mid": current})
            observations.append(external.observation_row(
                synthetic, observed_ts=decision_ts, source="kalshi", source_id="KXBTC-100K",
                source_event_ts=decision_ts, feature_name="external_probability",
                feature_value=sign * 0.08, q_external=current + sign * 0.08,
                confidence=0.9, mapping_score=0.95,
            ))
            prices.extend([
                {"schema": external.PRICE_SCHEMA, "observed_ts": decision_ts, "market_id": market.market_id,
                 "bid": bid, "ask": ask, "mid": current},
                {"schema": external.PRICE_SCHEMA, "observed_ts": decision_ts + 3600, "market_id": market.market_id,
                 "bid": future_bid, "ask": future_ask, "mid": future},
            ])
        return observations, prices

    def test_purged_walk_forward_and_cost_gates(self) -> None:
        observations, prices = self.synthetic_backtest_data()
        config = json.loads(json.dumps(self.config))
        config["backtest"].update({
            "horizons_seconds": [3600], "future_price_tolerance_seconds": 60,
            "min_train_observations": 8, "bootstrap_reps": 200, "extra_cost_bps": 5.0,
        })
        config["gates"].update({
            "min_oos_predictions": 40, "min_trades": 30,
            "max_bootstrap_pvalue": 0.10, "min_positive_fold_fraction": 0.75,
        })
        labeled = external.label_observations(observations, prices, 3600, 60)
        self.assertGreater(len(labeled), 80)
        for index in range(1, len(labeled)):
            decision = int(labeled[index]["observed_ts"])
            self.assertTrue(all(int(row["future_ts"]) < decision for row in external.purged_training_rows(labeled, index)))
        candidates = external.run_backtests(observations, prices, config)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertTrue(candidate["gate_pass"], candidate["reasons"])
        self.assertGreater(candidate["metrics"]["mse_improvement"], 0.0)
        self.assertGreater(candidate["metrics"]["cost_stress_net_pnl"]["2.0"], 0.0)
        self.assertLessEqual(candidate["raw_pvalue"], 0.10)
        evidence = external.alpha_evidence(candidates)
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertFalse(evidence["integration_evidence_pass"])

    def test_storage_is_deterministic_and_deduplicated(self) -> None:
        row = {"observation_id": "same", "observed_ts": self.now, "retrieved_ts": self.now,
               "market_id": "pm-1", "source": "kalshi", "feature_name": "external_probability"}
        newer = dict(row, retrieved_ts=self.now + 1, feature_value=0.1)
        merged = external.merge_rows([row], [newer, newer], key_fields=("observation_id",),
                                     min_timestamp=self.now - 1, max_rows=10)
        self.assertEqual(merged, [newer])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.jsonl.gz"
            external.write_jsonl_gz(path, merged)
            first = path.read_bytes()
            external.write_jsonl_gz(path, merged)
            self.assertEqual(first, path.read_bytes())
            self.assertEqual(external.read_jsonl_gz(path), merged)
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                self.assertEqual(len(handle.readlines()), 1)

    def test_demo_cli_is_paper_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty.jsonl.gz"
            external.write_jsonl_gz(empty, [])
            state = root / "state.json"
            state.write_text("{}\n", encoding="utf-8")
            report = root / "report.json"
            command = [
                "python3", str(ROOT / "scripts" / "external_intelligence.py"),
                "--config", str(ROOT / "config" / "external_intelligence.json"),
                "--observations-in", str(empty), "--prices-in", str(empty), "--state-in", str(state),
                "--observations-out", str(root / "observations.jsonl.gz"),
                "--prices-out", str(root / "prices.jsonl.gz"), "--state-out", str(root / "state-out.json"),
                "--signals-out", str(root / "signals.jsonl"), "--report-json", str(report),
                "--report-markdown", str(root / "report.md"), "--mode", "demo", "--now", str(self.now),
            ]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
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
