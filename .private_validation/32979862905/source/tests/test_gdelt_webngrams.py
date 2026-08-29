#!/usr/bin/env python3
from __future__ import annotations

import gzip
import io
import math
import sys
import unittest
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gdelt_webngrams as webngrams  # noqa: E402


@dataclass
class Market:
    market_id: str
    question: str


class FakeModule:
    MONTHS = {"august"}

    @staticmethod
    def integer(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def finite(value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if math.isfinite(parsed) else default

    @staticmethod
    def gdelt_query(question: str) -> str:
        return question.lower()

    @staticmethod
    def observation_row(
        market: Market,
        *,
        observed_ts: int,
        source: str,
        source_id: str,
        source_event_ts: int,
        feature_name: str,
        feature_value: float,
        confidence: float,
        mapping_score: float,
        metadata: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "market_id": market.market_id,
            "observed_ts": observed_ts,
            "source": source,
            "source_id": source_id,
            "source_event_ts": source_event_ts,
            "feature_name": feature_name,
            "feature_value": feature_value,
            "confidence": confidence,
            "mapping_score": mapping_score,
            "metadata": metadata,
        }


class GdeltWebNgramsTests(unittest.TestCase):
    def test_scanner_requires_configured_token_overlap(self) -> None:
        lines = [
            b"1\tbitcoin price rises sharply\t3\n",
            b"2\tbitcoin unrelated coverage today\t5\n",
            b"3\tbrent oil shipping disruption\t2\n",
        ]
        documents, mentions = webngrams._scan_ngram_lines(
            lines,
            [{"bitcoin", "price"}, {"brent", "oil"}],
            min_token_matches=2,
        )
        self.assertEqual(documents, [{"1"}, {"3"}])
        self.assertEqual(mentions, [3, 2])

    def test_collection_uses_recent_downloadable_ngram_file(self) -> None:
        now = int(datetime(2026, 8, 25, 12, 55, tzinfo=timezone.utc).timestamp())
        expected_ts = int(datetime(2026, 8, 25, 12, 50, tzinfo=timezone.utc).timestamp())
        payload = gzip.compress(
            b"1\tbitcoin price rises sharply\t3\n"
            b"2\tbitcoin price volatility grows\t2\n"
            b"3\tbrent oil shipping disruption\t4\n"
        )

        def opener(request: Any, timeout: float = 0.0) -> io.BytesIO:
            self.assertGreater(timeout, 0.0)
            if "20260825125000.ngrams.txt.gz" in request.full_url:
                return io.BytesIO(payload)
            raise urllib.error.HTTPError(request.full_url, 404, "missing", None, None)

        config = {
            "sources": {
                "gdelt": {
                    "enabled": True,
                    "markets_per_run": 2,
                    "base_confidence": 0.35,
                    "webngrams_min_age_minutes": 5,
                    "webngrams_lookback_minutes": 10,
                    "webngrams_min_token_matches": 2,
                }
            }
        }
        observations, health, errors = webngrams.collect_gdelt_webngrams(
            [Market("btc", "bitcoin price"), Market("oil", "brent oil")],
            config,
            now,
            FakeModule,
            opener=opener,
        )
        self.assertEqual(errors, [])
        self.assertEqual(health["_transport"]["transport"], "webngrams")
        self.assertEqual(health["_transport"]["dataset_ts"], expected_ts)
        by_market = {row["market_id"]: row for row in observations}
        self.assertEqual(set(by_market), {"btc", "oil"})
        self.assertEqual(by_market["btc"]["source"], "gdelt")
        self.assertEqual(by_market["btc"]["feature_name"], "news_count_recent")
        self.assertAlmostEqual(by_market["btc"]["feature_value"], math.log1p(2))
        self.assertAlmostEqual(by_market["oil"]["feature_value"], math.log1p(1))

    def test_candidate_minutes_are_old_enough_and_bounded(self) -> None:
        now = 10_000
        values = webngrams._candidate_minutes(now, min_age_minutes=5, lookback_minutes=7)
        rounded = (now // 60) * 60
        self.assertEqual(values, [rounded - 300, rounded - 360, rounded - 420])


if __name__ == "__main__":
    unittest.main()
