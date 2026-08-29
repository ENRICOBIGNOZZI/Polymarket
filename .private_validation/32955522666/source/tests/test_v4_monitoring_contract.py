from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LiveMonitoringContractTest(unittest.TestCase):
    def test_strategy_a_producer_matches_exporter_filename(self):
        exporter = (ROOT / "monitoring" / "exporter.py").read_text(encoding="utf-8")
        once = (ROOT / "scripts" / "paper_v4_once.sh").read_text(encoding="utf-8")
        loop = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("structural_latest.csv", exporter)
        for producer in (once, loop, smoke):
            self.assertIn("structural_latest.csv", producer)

    def test_live_smoke_resolves_the_champion_manifest(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("config/live_champion.json", smoke)
        self.assertIn("CHAMPION_VERSION", smoke)
        self.assertIn("CHAMPION_CONFIG", smoke)
        self.assertIn("CHAMPION_RUN_NAME", smoke)
        self.assertIn("version not in (5,6)", smoke)
        self.assertIn("Fresh public-data V5 full-strategy smoke", smoke)
        self.assertIn("Fresh public-data V6 model-specific smoke", smoke)

    def test_v5_smoke_still_exercises_real_v5_children_when_selected(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("if: env.CHAMPION_VERSION == '5'", smoke)
        self.assertIn("R=paper_v5_live", smoke)
        self.assertIn("scripts/multi_strategy_paper.py", smoke)
        self.assertIn("--markets 180 --min-liquidity 25 --once", smoke)
        self.assertNotIn("--scan-only --once", smoke)
        self.assertIn("polymarket_trade_recorder", smoke)
        self.assertIn('--trade-tape "$R/trade_tape.csv"', smoke)
        run_pos = smoke.index('| tee "$R/allocator_once.log"')
        metrics_pos = smoke.index("from exporter_latest import LatestCollector")
        self.assertLess(run_pos, metrics_pos)
        self.assertIn('adapter="v5"', smoke)
        self.assertIn("polymarket_allocator_state_present 1", smoke)

    def test_v6_smoke_exercises_model_specific_runtime_when_selected(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        helper = (ROOT / "scripts" / "v6_live_smoke_once.sh").read_text(encoding="utf-8")
        self.assertIn("if: env.CHAMPION_VERSION == '6'", smoke)
        self.assertIn("scripts/v6_live_smoke_once.sh", smoke)
        self.assertIn("paper_v6_live", smoke)
        self.assertIn("v6_materialize_configs.py", helper)
        self.assertIn("v6_micro_taker.py", helper)
        self.assertIn("v6_hard_arb_paper.py", helper)
        self.assertIn("v6_local_factor_intents.py", helper)
        self.assertIn("v6_relation_intents.py", helper)
        self.assertIn("v6_intent_guard.py", helper)
        self.assertIn("polymarket_multileg_paper", helper)
        self.assertIn("v6_external_bridge.py", helper)
        self.assertIn('adapter="v6"', helper)
        self.assertIn("polymarket_v6_exporter_info", helper)

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
        self.assertIn("- name: Build public telemetry snapshot\n        if: always()", smoke)
        self.assertIn("if: github.event_name != 'pull_request' && success()", smoke)

    def test_validated_ref_advances_only_after_selected_champion_telemetry(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        verify_pos = smoke.index("- name: Verify selected champion telemetry")
        snapshot_pos = smoke.index("- name: Build public telemetry snapshot")
        advance_pos = smoke.index("- name: Advance paper validated ref")
        self.assertLess(verify_pos, snapshot_pos)
        self.assertLess(snapshot_pos, advance_pos)
        self.assertIn("test \"$validated_sha\" = \"$main_sha\"", smoke)
        self.assertIn("-F force=false", smoke)

    def test_trade_tape_and_explicit_horizon_are_preserved(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        helper = (ROOT / "scripts" / "v6_live_smoke_once.sh").read_text(encoding="utf-8")
        self.assertIn("--lookback-seconds 900", smoke)
        self.assertIn("--lookback-seconds 900", helper)
        self.assertIn('--trade-tape "$R/trade_tape.csv"', smoke)
        self.assertIn('--trade-tape "$R/trade_tape.csv"', helper)
        self.assertIn("--horizon-seconds 5", helper)
        self.assertIn("--trade-lookback-seconds 900", smoke)

    def test_pca_hedge_cap_matches_legacy_runtime_only(self):
        v4_once = (ROOT / "scripts" / "paper_v4_once.sh").read_text(encoding="utf-8")
        v4_loop = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        v5_loop = (ROOT / "scripts" / "paper_v5_loop.sh").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        v6_loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        for producer in (v4_once, v4_loop):
            self.assertIn("--max-hedges 4", producer)
        for producer in (v5_loop, smoke):
            self.assertIn("--max-hedges 8", producer)
        self.assertNotIn("polymarket_pca_stat_arb", v6_loop)

    def test_v6_telemetry_is_native_with_legacy_health_compatibility_only(self):
        exporter = (ROOT / "monitoring" / "exporter_v6.py").read_text(encoding="utf-8")
        status = (ROOT / "scripts" / "v6_runtime_status.py").read_text(encoding="utf-8")
        self.assertIn("polymarket_v6_exporter_info", exporter)
        self.assertIn("polymarket_v6_local_factor_clusters", exporter)
        self.assertIn("v6_legacy_health_view", status)
        self.assertIn("no V5 expert or mixture", status)
        self.assertIn("models_expected", status)


if __name__ == "__main__":
    unittest.main()
