from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "v6-market-cache-relay.yml"


class V6MarketCacheRelayContractTests(unittest.TestCase):
    def test_relay_is_scheduled_read_only_for_code_and_atomic_for_cache(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', workflow)
        self.assertIn("  push:\n    branches: [main]", workflow)
        self.assertIn('".github/workflows/v6-market-cache-relay.yml"', workflow)
        self.assertIn('"tests/test_v6_market_cache_relay_contract.py"', workflow)
        self.assertIn("permissions:\n  contents: read\n", workflow)
        self.assertIn("scripts/v6_market_snapshot.py", workflow)
        self.assertIn("market_proxy_cache.json.relay.${GITHUB_RUN_ID}", workflow)
        self.assertIn('mv "$incoming" "$target"', workflow)
        self.assertIn('time.time()-float(value["timestamp"]) <= 120', workflow)
        self.assertIn('paper_validated_sha="$(git rev-parse origin/paper-validated)"', workflow)
        self.assertIn('test "$(git rev-parse HEAD)" = "$paper_validated_sha"', workflow)
        self.assertIn('git checkout --detach "$PAPER_VALIDATED_SHA"', workflow)
        self.assertIn('test "$(git rev-parse HEAD)" = "$PAPER_VALIDATED_SHA"', workflow)
        self.assertIn('paper_validated_sha=', workflow)
        for forbidden in ("gh pr merge", "git push origin", "POLYMARKET_DEPLOY_REF=", "contents: write"):
            self.assertNotIn(forbidden, workflow)

    def test_relay_fails_closed_unless_running_proxy_consumes_installed_cache(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Verify running proxy consumes installed cache", workflow)
        self.assertIn("$RUN_ROOT_REL/runtime_config.json", workflow)
        self.assertIn("parsed.hostname in {'127.0.0.1','localhost'}", workflow)
        self.assertIn('curl -fsS --max-time 3 "$proxy_url/healthz"', workflow)
        self.assertIn("liquidity_num_min=10", workflow)
        self.assertIn("isinstance(rows,list) and rows", workflow)
        self.assertIn("polymarket_v6_market_proxy_status_v1", workflow)
        self.assertIn("endswith('_cache')", workflow)
        self.assertIn("cache_age_seconds", workflow)
        self.assertIn("live_proxy_cache_consumed=true", workflow)
        self.assertIn("relay-evidence/live-proxy-consumption.txt", workflow)
        for forbidden in (
            "polymarket-service-control restart",
            "kill -TERM",
            "kill -KILL",
            "paper-validated --force",
        ):
            self.assertNotIn(forbidden, workflow)

    def test_relay_tolerates_only_a_bounded_planned_restart_window(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("for attempt in {1..18}; do", workflow)
        self.assertIn("relay_verify_retry=", workflow)
        self.assertIn("relay_verify_attempt=", workflow)
        self.assertIn("sleep 5", workflow)
        self.assertIn('test "$verified" = true', workflow)
        self.assertIn("bounded 90 seconds", workflow)
        self.assertNotIn("continue-on-error: true", workflow)


if __name__ == "__main__":
    unittest.main()
