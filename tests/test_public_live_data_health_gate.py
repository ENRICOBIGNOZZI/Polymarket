from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "v4-live-smoke.yml"


class PublicLiveDataHealthGateTest(unittest.TestCase):
    def test_unhealthy_public_recorder_is_published_then_fails_validation(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        publish = text.index("      - name: Publish latest public telemetry")
        enforce = text.index("      - name: Enforce public trade-recorder data health")
        advance = text.index("      - name: Advance paper validated ref")

        self.assertLess(publish, enforce)
        self.assertLess(enforce, advance)

        gate = text[enforce:advance]
        self.assertIn("live_snapshot.json", gate)
        self.assertIn("data_health", gate)
        self.assertIn("trade_recorder", gate)
        self.assertIn("status != 'healthy'", gate)

        publish_block = text[publish:enforce]
        self.assertIn("github.event_name != 'pull_request' && success()", publish_block)
        self.assertIn("telemetry/latest-live-smoke.json", publish_block)

        advance_block = text[advance:]
        self.assertIn("github.event_name != 'pull_request' && success()", advance_block)

    def test_snapshot_builder_preserves_unhealthy_diagnostics(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        build = text.index("      - name: Build public telemetry snapshot")
        publish = text.index("      - name: Publish live summary")
        block = text[build:publish]
        self.assertIn("if: always()", block)
        self.assertIn("scripts/summarize_live_smoke.py", block)
        self.assertIn("|| true", block)


if __name__ == "__main__":
    unittest.main()
