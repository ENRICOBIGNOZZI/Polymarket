from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PointInTimeArchiveWorkflowContractTest(unittest.TestCase):
    def test_v7_data_plane_schedulers_are_registered_without_v6_compatibility(self) -> None:
        registry = json.loads((ROOT / "config/scheduler_registry.json").read_text())
        rows = {row["id"]: row for row in registry["schedulers"]}

        archive = rows["v7-point-in-time-universe-archive"]
        self.assertEqual(archive["workflow"], ".github/workflows/v7-point-in-time-universe-archive.yml")
        self.assertEqual(archive["workflow_name"], "V7 point-in-time universe archive")
        self.assertEqual(archive["job"], "archive")
        self.assertFalse(archive["merge_authority"])
        self.assertFalse(archive["deploy_authority"])
        self.assertFalse(archive["validation_dispatch_authority"])
        responsibility = archive["responsibility"].lower()
        self.assertIn("immutable", responsibility)
        self.assertIn("point-in-time", responsibility)
        self.assertIn("without champion, execution, pnl or risk mutation", responsibility)

        relay = rows["v7-market-cache-relay"]
        self.assertEqual(relay["workflow"], ".github/workflows/v7-market-cache-relay.yml")
        self.assertEqual(relay["workflow_name"], "V7 market cache relay")
        self.assertEqual(relay["job"], "relay")
        self.assertTrue(relay["critical"])
        self.assertFalse(relay["merge_authority"])
        self.assertFalse(relay["deploy_authority"])
        self.assertFalse(relay["validation_dispatch_authority"])
        self.assertNotIn("v6-live-data-research", rows)
        self.assertNotIn("v6-market-cache-relay", rows)

    def test_archive_is_v7_only_and_reads_execution_cache(self) -> None:
        workflow = (ROOT / ".github/workflows/v7-point-in-time-universe-archive.yml").read_text()
        self.assertIn('cron: "11,41 * * * *"', workflow)
        self.assertIn("git fetch -q origin paper-validated", workflow)
        self.assertIn("git rev-parse origin/paper-validated", workflow)
        self.assertIn('git show "${validated_sha}:config/live_champion.json"', workflow)
        self.assertIn('if version != 7:', workflow)
        self.assertIn('execution_root="$run_root/execution"', workflow)
        self.assertIn('cache="$execution_root/market_proxy_cache.json"', workflow)
        self.assertIn("polymarket_v7_market_proxy_cache_v1", workflow)
        self.assertNotIn("polymarket_v6_market_proxy_cache_v1", workflow)
        self.assertIn('git cat-file -e "${validated_sha}:${helper_path}"', workflow)
        self.assertIn('git show "${validated_sha}:${helper_path}"', workflow)
        self.assertIn("--cadence-seconds 1800", workflow)
        self.assertIn("--retention-days 45", workflow)
        self.assertNotIn("gamma-api.polymarket.com", workflow)
        self.assertNotIn("clob.polymarket.com", workflow)
        self.assertNotIn("batch-prices-history", workflow)
        self.assertNotIn("submit_order", workflow.lower())
        self.assertNotIn("live_champion.json' >", workflow)

    def test_v7_cache_relay_is_native_and_targets_execution_runtime(self) -> None:
        path = ROOT / ".github/workflows/v7-market-cache-relay.yml"
        self.assertTrue(path.is_file())
        workflow = path.read_text()
        self.assertIn("name: V7 market cache relay", workflow)
        self.assertIn('cron: "*/5 * * * *"', workflow)
        self.assertIn("python3 scripts/v7_market_snapshot.py", workflow)
        self.assertIn("polymarket_v7_market_proxy_cache_v1", workflow)
        self.assertIn("polymarket_v7_market_proxy_status_v1", workflow)
        self.assertIn('execution_root="$RUN_ROOT_REL/execution"', workflow)
        self.assertIn("validated_champion_not_v7", (ROOT / ".github/workflows/v7-point-in-time-universe-archive.yml").read_text())
        self.assertNotIn("scripts/v6_", workflow)
        self.assertNotIn("config/paper_v6.json", workflow)
        self.assertNotIn("polymarket_v6_", workflow)
        self.assertFalse((ROOT / ".github/workflows/v6-market-cache-relay.yml").exists())
        self.assertFalse((ROOT / ".github/workflows/v6-research-smoke.yml").exists())

    def test_helper_is_append_only_per_bucket(self) -> None:
        source = (ROOT / "scripts/v7_archive_market_universe.py").read_text()
        self.assertIn("if not target.exists():", source)
        self.assertIn("os.link(tmp, target)", source)
        self.assertNotIn("os.replace(tmp, target)", source)
        self.assertIn('archive_dir.glob("universe-*.json.gz")', source)


if __name__ == "__main__":
    unittest.main()
