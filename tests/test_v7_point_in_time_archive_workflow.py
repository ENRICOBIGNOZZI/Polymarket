from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PointInTimeArchiveWorkflowContractTest(unittest.TestCase):
    def test_scheduler_is_registered_and_data_only(self) -> None:
        registry = json.loads((ROOT / "config/scheduler_registry.json").read_text())
        rows = {row["id"]: row for row in registry["schedulers"]}
        row = rows["v7-point-in-time-universe-archive"]
        self.assertEqual(row["workflow"], ".github/workflows/v7-point-in-time-universe-archive.yml")
        self.assertEqual(row["workflow_name"], "V7 point-in-time universe archive")
        self.assertEqual(row["job"], "archive")
        self.assertFalse(row["merge_authority"])
        self.assertFalse(row["deploy_authority"])
        self.assertFalse(row["validation_dispatch_authority"])
        responsibility = row["responsibility"].lower()
        self.assertIn("immutable", responsibility)
        self.assertIn("point-in-time", responsibility)
        self.assertIn("without champion, execution, pnl or risk mutation", responsibility)

    def test_workflow_reads_only_v7_cache_and_exact_validated_helper(self) -> None:
        workflow = (ROOT / ".github/workflows/v7-point-in-time-universe-archive.yml").read_text()
        self.assertIn('cron: "11,41 * * * *"', workflow)
        self.assertIn("git fetch -q origin paper-validated", workflow)
        self.assertIn("git rev-parse origin/paper-validated", workflow)
        self.assertIn('git show "${validated_sha}:config/live_champion.json"', workflow)
        self.assertIn('version != 7', workflow)
        self.assertIn('git show "${validated_sha}:${helper_path}"', workflow)
        self.assertIn('cache="$run_root/execution/market_proxy_cache.json"', workflow)
        self.assertIn("polymarket_v7_market_proxy_cache_v1", workflow)
        self.assertIn("--cadence-seconds 1800", workflow)
        self.assertIn("--retention-days 45", workflow)
        self.assertNotIn("polymarket_v6", workflow)
        self.assertNotIn("gamma-api.polymarket.com", workflow)
        self.assertNotIn("clob.polymarket.com", workflow)
        self.assertNotIn("batch-prices-history", workflow)
        self.assertNotIn("submit_order", workflow.lower())
        self.assertNotIn("live_champion.json' >", workflow)

    def test_helper_is_append_only_per_bucket(self) -> None:
        source = (ROOT / "scripts/v7_archive_market_universe.py").read_text()
        self.assertIn("if not target.exists():", source)
        self.assertIn("os.link(tmp, target)", source)
        self.assertNotIn("os.replace(tmp, target)", source)
        self.assertIn('archive_dir.glob("universe-*.json.gz")', source)


if __name__ == "__main__":
    unittest.main()
