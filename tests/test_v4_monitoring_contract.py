from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V4MonitoringContractTest(unittest.TestCase):
    def test_strategy_a_producer_matches_exporter_filename(self):
        exporter = (ROOT / "monitoring" / "exporter.py").read_text(encoding="utf-8")
        once = (ROOT / "scripts" / "paper_v4_once.sh").read_text(encoding="utf-8")
        loop = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")

        self.assertIn("structural_latest.csv", exporter)
        for producer in (once, loop, smoke):
            self.assertIn("structural_latest.csv", producer)

    def test_live_smoke_exercises_v4_adapter_not_base_fallback(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("R=paper_v4_live", smoke)
        self.assertIn("'paper_v4_live', 'config/paper_v4.json'", smoke)
        self.assertIn('adapter="v4"', smoke)
        self.assertIn('polymarket_multileg_state_present 1', smoke)
        self.assertIn('polymarket_oos_state_present 1', smoke)

    def test_live_smoke_runs_after_main_merge_and_hourly(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("push:", smoke)
        self.assertIn("branches: [main]", smoke)
        self.assertIn('cron: "37 * * * *"', smoke)

    def test_live_smoke_publishes_public_telemetry_checkpoint(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/summarize_live_smoke.py", smoke)
        self.assertIn("telemetry/latest-live-smoke.json", smoke)
        self.assertIn("branch=telemetry", smoke)
        self.assertIn("github.event_name != 'pull_request'", smoke)
        self.assertIn("continue-on-error: true", smoke)

    def test_pca_sparse_hedge_cap_matches_production_and_smoke(self):
        once = (ROOT / "scripts" / "paper_v4_once.sh").read_text(encoding="utf-8")
        loop = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        for producer in (once, loop, smoke):
            self.assertIn("--max-hedges 4", producer)

    def test_b1_shadow_fillability_is_non_blocking_and_separate_from_production(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("B1 shadow fillability diagnostic", smoke)
        self.assertIn('SH="$R/shadow_b1"', smoke)
        self.assertIn("--min-z 1.25", smoke)
        self.assertIn('continue-on-error: true', smoke)
        self.assertIn('paper_v4_live/shadow_b1', smoke)
        production_segment = smoke.split("B1 shadow fillability diagnostic", 1)[0]
        self.assertIn("--min-z 1.5", production_segment)


if __name__ == "__main__":
    unittest.main()
