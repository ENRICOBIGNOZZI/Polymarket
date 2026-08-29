from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import v6_market_snapshot as snapshot


def market(index: int, liquidity: float = 25.0) -> dict[str, object]:
    return {
        "id": str(index),
        "conditionId": str(index),
        "liquidityNum": liquidity,
    }


class FakeProxy:
    def __init__(self, gamma: str, clob: str, cache: Path, status: Path):
        self.gamma = gamma.rstrip("/")
        self.clob = clob.rstrip("/")
        self.cache = cache
        self.status = status
        self.source = "startup"
        self.error = ""
        self.failures = 0

    def gamma_rows(self, *_args, **_kwargs):
        raise RuntimeError("keyset unavailable in deterministic test")

    def clob_rows(self, *_args, **_kwargs):
        raise RuntimeError("clob unavailable in deterministic test")

    def save(self, rows):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(
            json.dumps(
                {
                    "schema": "polymarket_v6_market_proxy_cache_v1",
                    "timestamp": int(time.time()),
                    "markets": rows,
                    "gamma_to_condition": {
                        str(row.get("id") or ""): str(row.get("conditionId") or "")
                        for row in rows
                        if isinstance(row, dict)
                    },
                }
            ),
            encoding="utf-8",
        )

    def stat(self, source: str, n: int, upstream_ok: bool, age: float = 0.0):
        self.source = source
        self.status.parent.mkdir(parents=True, exist_ok=True)
        self.status.write_text(
            json.dumps(
                {
                    "schema": "polymarket_v6_market_proxy_status_v1",
                    "timestamp": int(time.time()),
                    "source": source,
                    "markets": n,
                    "upstream_gamma_ok": upstream_ok,
                    "failures": self.failures,
                    "last_error": self.error,
                    "cache_age_seconds": age,
                    "paper_only": True,
                }
            ),
            encoding="utf-8",
        )


class V6MarketSnapshotTests(unittest.TestCase):
    def test_snapshot_writes_and_validates_fresh_relay_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "cache.json"
            status = root / "status.json"

            def page(_gamma, offset, limit, _min_liquidity):
                return offset, [market(offset + index) for index in range(limit)], []

            with mock.patch.object(snapshot, "Proxy", FakeProxy), mock.patch.object(
                snapshot, "_gamma_page", side_effect=page
            ):
                rc = snapshot.main(
                    [
                        "--output",
                        str(output),
                        "--status",
                        str(status),
                        "--markets",
                        "2",
                        "--min-markets",
                        "2",
                        "--min-liquidity",
                        "10",
                    ]
                )

            self.assertEqual(rc, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "polymarket_v6_market_proxy_cache_v1")
            self.assertEqual(len(payload["markets"]), 2)

    def test_one_valid_page_is_accepted_when_it_meets_explicit_minimum(self) -> None:
        proxy = FakeProxy("https://gamma.test", "https://clob.test", Path("unused"), Path("unused.status"))

        def page(_gamma, offset, _limit, _min_liquidity):
            if offset == 0:
                return offset, [market(index) for index in range(100)], []
            return offset, [], [f"offset={offset}:timeout"]

        with mock.patch.object(snapshot, "_gamma_page", side_effect=page):
            rows, source, errors = snapshot.build_fresh_rows(proxy, 300, 100, 10.0)

        self.assertEqual(len(rows), 100)
        self.assertTrue(source.endswith("_partial"))
        self.assertTrue(errors)
        self.assertEqual(len({row["conditionId"] for row in rows}), 100)

    def test_partial_pages_below_minimum_still_fail_closed(self) -> None:
        proxy = FakeProxy("https://gamma.test", "https://clob.test", Path("unused"), Path("unused.status"))

        def page(_gamma, offset, _limit, _min_liquidity):
            if offset == 0:
                return offset, [market(index) for index in range(99)], []
            return offset, [], [f"offset={offset}:timeout"]

        with mock.patch.object(snapshot, "_gamma_page", side_effect=page):
            with self.assertRaisesRegex(RuntimeError, "only 99 markets; need 100"):
                snapshot.build_fresh_rows(proxy, 300, 100, 10.0)

    def test_snapshot_rejects_incomplete_persisted_cache_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "cache.json"
            status = root / "status.json"

            class BrokenPersistProxy(FakeProxy):
                def save(self, rows):
                    del rows
                    self.cache.write_text(
                        json.dumps(
                            {
                                "schema": "polymarket_v6_market_proxy_cache_v1",
                                "timestamp": int(time.time()),
                                "markets": [{"id": "broken", "liquidityNum": 25.0}],
                            }
                        ),
                        encoding="utf-8",
                    )

            with mock.patch.object(snapshot, "Proxy", BrokenPersistProxy), mock.patch.object(
                snapshot,
                "build_fresh_rows",
                return_value=([market(1)], "gamma_legacy_retried", []),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid market rows"):
                    snapshot.main(
                        [
                            "--output",
                            str(output),
                            "--status",
                            str(status),
                            "--markets",
                            "1",
                            "--min-markets",
                            "1",
                            "--min-liquidity",
                            "10",
                        ]
                    )


if __name__ == "__main__":
    unittest.main()
