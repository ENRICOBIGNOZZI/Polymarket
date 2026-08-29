from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "v6-market-cache-relay.yml"


class V6MarketCacheRelayContractTests(unittest.TestCase):
    def test_relay_is_scheduled_read_only_for_code_and_atomic_for_cache(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', workflow)
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertIn("scripts/v6_market_snapshot.py", workflow)
        self.assertIn("market_proxy_cache.json.relay.${GITHUB_RUN_ID}", workflow)
        self.assertIn('mv "$incoming" "$target"', workflow)
        self.assertIn('time.time()-float(value["timestamp"]) <= 120', workflow)
        self.assertIn('test "$(git rev-parse HEAD)" = "$(git rev-parse origin/paper-validated)"', workflow)
        for forbidden in ("gh pr merge", "git push origin", "POLYMARKET_DEPLOY_REF=", "contents: write"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()
