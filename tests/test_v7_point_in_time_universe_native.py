from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_archive_market_universe as archive

SHA = "d" * 40


def raw_market(mid: int, liquidity: float = 10.0) -> dict:
    return {
        "id": str(mid),
        "conditionId": f"c{mid}",
        "clobTokenIds": json.dumps([f"y{mid}", f"n{mid}"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "liquidityNum": liquidity,
        "volume24hr": 5.0,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "events": [{"id": f"e{mid}"}],
        "question": f"Question {mid}",
    }


class NativeUniverseArchiveTest(unittest.TestCase):
    def test_normalized_market_has_native_v7_membership_identity(self) -> None:
        row = archive.normalized_market(raw_market(1))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["market_id"], "1")
        self.assertEqual(row["condition_id"], "c1")
        self.assertEqual(row["event_ids"], ["e1"])
        self.assertEqual(row["clob_token_ids"], ["y1", "n1"])

    def test_discovery_is_direct_gamma_and_filters_liquidity(self) -> None:
        calls = []
        def fake(url: str):
            calls.append(url)
            return [raw_market(1, 20.0), raw_market(2, 1.0)]
        rows = archive.discover("https://gamma.example", market_limit=2, min_liquidity=2.0, page_size=100, fetcher=fake)
        self.assertEqual([row["market_id"] for row in rows], ["1"])
        self.assertEqual(len(calls), 1)
        self.assertIn("active=true", calls[0])
        self.assertIn("closed=false", calls[0])

    def test_snapshot_is_exact_sha_paper_only_and_hashes_membership(self) -> None:
        markets = [archive.normalized_market(raw_market(1)), archive.normalized_market(raw_market(2))]
        rows = [row for row in markets if row is not None]
        value = archive.snapshot(rows, model_sha=SHA, captured_ts_ms=1_000_000, gamma_url="https://gamma.example", market_limit=1000, min_liquidity=2.0, cadence_seconds=1800)
        self.assertEqual(value["schema"], archive.SCHEMA)
        self.assertEqual(value["model_sha"], SHA)
        self.assertTrue(value["paper_only"])
        self.assertFalse(value["authenticated_execution"])
        self.assertEqual(value["market_count"], 2)
        self.assertEqual(len(value["membership_sha256"]), 64)

    def test_archive_is_immutable_and_latest_is_native_v7(self) -> None:
        row = archive.normalized_market(raw_market(1))
        assert row is not None
        value = archive.snapshot([row], model_sha=SHA, captured_ts_ms=2_000_000, gamma_url="https://gamma.example", market_limit=1000, min_liquidity=2.0, cadence_seconds=1800)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = archive.write_snapshot(root, value, retention_days=45)
            same = archive.write_snapshot(root, value, retention_days=45)
            self.assertEqual(path, same)
            loaded = json.loads(gzip.decompress((root / "latest.json.gz").read_bytes()).decode("utf-8"))
            self.assertEqual(loaded["schema"], archive.SCHEMA)
            changed = dict(value); changed["market_count"] = 2
            with self.assertRaises(ValueError):
                archive.write_snapshot(root, changed, retention_days=45)

    def test_workflow_contains_only_v7_cache_and_champion_contract(self) -> None:
        text = (ROOT / ".github" / "workflows" / "v7-point-in-time-universe-archive.yml").read_text(encoding="utf-8")
        self.assertIn("v7_archive_market_universe.py", text)
        self.assertIn("polymarket_v7_point_in_time_universe_v2", text)
        self.assertIn("V7", text)
        self.assertNotIn("version not in {6,7}", text)
        self.assertNotIn("market_proxy_cache.json", text)


if __name__ == "__main__":
    unittest.main()
