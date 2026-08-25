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


class V6MarketSnapshotTests(unittest.TestCase):
    def test_snapshot_writes_and_validates_fresh_relay_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "cache.json"
            status = root / "status.json"

            class FakeProxy:
                def __init__(self, gamma: str, clob: str, cache: Path, state: Path):
                    del gamma, clob, state
                    self.cache = cache
                    self.source = "gamma_legacy"

                def markets(self, query: dict[str, list[str]]) -> list[dict[str, object]]:
                    count = int(query["limit"][0])
                    rows = [
                        {
                            "id": str(index),
                            "conditionId": str(index),
                            "liquidityNum": 25.0,
                        }
                        for index in range(count)
                    ]
                    self.cache.write_text(
                        json.dumps(
                            {
                                "schema": "polymarket_v6_market_proxy_cache_v1",
                                "timestamp": int(time.time()),
                                "markets": rows,
                                "gamma_to_condition": {str(index): str(index) for index in range(count)},
                            }
                        ),
                        encoding="utf-8",
                    )
                    return rows

            with mock.patch.object(snapshot, "Proxy", FakeProxy):
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


if __name__ == "__main__":
    unittest.main()
