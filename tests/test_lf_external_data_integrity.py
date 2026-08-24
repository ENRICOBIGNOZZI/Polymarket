#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lf_external_data_integrity", ROOT / "scripts" / "lf_external_data_integrity.py"
)
assert SPEC and SPEC.loader
integrity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = integrity
SPEC.loader.exec_module(integrity)


class LfExternalDataIntegrityTests(unittest.TestCase):
    def test_clob_absolute_window_excludes_interval(self) -> None:
        params = integrity.build_clob_absolute_history_params(
            "123456789", 1_700_000_000, 1_700_086_400, 60
        )
        self.assertEqual(params["market"], "123456789")
        self.assertEqual(params["startTs"], 1_700_000_000)
        self.assertEqual(params["endTs"], 1_700_086_400)
        self.assertEqual(params["fidelity"], 60)
        self.assertNotIn("interval", params)
        url = integrity.build_clob_absolute_history_url(
            "123456789", 1_700_000_000, 1_700_086_400, 60
        )
        self.assertIn("startTs=1700000000", url)
        self.assertIn("endTs=1700086400", url)
        self.assertNotIn("interval=", url)

    def test_kalshi_direct_market_query_excludes_multivariate(self) -> None:
        params = integrity.build_kalshi_direct_market_params(limit=2500, cursor="next")
        self.assertEqual(params["status"], "open")
        self.assertEqual(params["limit"], 1000)
        self.assertEqual(params["mve_filter"], "exclude")
        self.assertEqual(params["cursor"], "next")

    def test_current_failure_shape_is_classified_as_data_bound(self) -> None:
        report = {
            "generated_ts": 1_787_599_010,
            "collection": {
                "kalshi_markets": 2500,
                "kalshi_matches": 0,
                "source_errors": [
                    "request failed: https://clob.polymarket.com/prices-history?market=1&startTs=1&endTs=2&interval=1h&fidelity=60: HTTP Error 400: Bad Request",
                    "request failed: https://api.gdeltproject.org/api/v2/doc/doc?query=test: HTTP Error 429: Too Many Requests",
                ],
            },
            "backtest": {"candidate_count": 0},
            "mapping_diagnostics": [
                {
                    "kalshi_ticker": "KXMVECROSSCATEGORY-SHARD1-ABC",
                    "kalshi_title": "yes Chelsea,yes Fiorentina,yes Over 1.5 goals",
                },
                {
                    "kalshi_ticker": "KXMVECROSSCATEGORY-SHARD1-DEF",
                    "kalshi_title": "yes Boston,yes Chicago,yes Seattle",
                },
            ],
        }
        result = integrity.analyze_external_report(report)
        self.assertEqual(result["decision"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(result["model_evidence_ready"])
        self.assertIn("clob_absolute_history_filter_conflict", result["defects"])
        self.assertIn("kalshi_multivariate_sample_saturation", result["defects"])
        self.assertIn("gdelt_rate_limit_pressure", result["defects"])
        self.assertEqual(result["clob_history_400_errors"], 1)
        self.assertEqual(result["gdelt_429_errors"], 1)
        self.assertAlmostEqual(result["mve_diagnostic_fraction"], 1.0)

    def test_healthy_collection_does_not_invent_a_defect(self) -> None:
        report = {
            "generated_ts": 1_800_000_000,
            "collection": {
                "kalshi_markets": 1200,
                "kalshi_matches": 12,
                "source_errors": [],
            },
            "backtest": {"candidate_count": 3},
            "mapping_diagnostics": [
                {"kalshi_ticker": "KXBTC-100K", "kalshi_title": "Bitcoin above 100k"}
            ],
        }
        result = integrity.analyze_external_report(report)
        self.assertEqual(result["defects"], [])
        self.assertTrue(result["model_evidence_ready"])
        self.assertEqual(result["decision"], "EVIDENCE_AVAILABLE")


if __name__ == "__main__":
    unittest.main()
