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

    def test_live_smoke_exercises_every_v5_adapter_not_base_fallback(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("R=paper_v5_live", smoke)
        self.assertIn("'paper_v5_live', 'config/paper_v5.json'", smoke)
        self.assertIn('adapter=\"v5\"', smoke)
        self.assertIn("for model in ('micro', 'pca', 'graph', 'semantic', 'external')", smoke)
        self.assertIn('polymarket_model_info{{expert=\"{model}\",model=\"{model}\"}} 1', smoke)
        self.assertIn('polymarket_allocator_state_present 1', smoke)
        self.assertIn('polymarket_multileg_state_present 1', smoke)

    def test_live_smoke_runs_real_paper_children_before_export(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("--markets 500 --min-liquidity 10 --once", smoke)
        self.assertNotIn("--scan-only --once", smoke)
        self.assertIn("if line.startswith('polymarket_runtime_execution_staleness_seconds ')", smoke)
        self.assertIn("assert staleness < 3600.0, staleness", smoke)
        run_pos = smoke.index('| tee "$R/allocator_once.log"')
        metrics_pos = smoke.index("from exporter_latest import LatestCollector")
        self.assertLess(run_pos, metrics_pos)

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

    def test_validated_ref_advances_only_after_telemetry_snapshot_builds(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        snapshot_pos = smoke.index("- name: Build public telemetry snapshot")
        advance_pos = smoke.index("- name: Advance paper validated ref")
        self.assertLess(snapshot_pos, advance_pos)

    def test_multileg_uses_broad_recorded_trade_tape_and_explicit_horizon(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("polymarket_trade_recorder", smoke)
        self.assertIn("--markets 800 --batch 40 --min-liquidity 10 --lookback-seconds 900", smoke)
        self.assertIn('--trade-tape "$R/trade_tape.csv"', smoke)
        self.assertIn("--trade-lookback-seconds 900", smoke)

    def test_pca_hedge_cap_matches_each_runtime_generation(self):
        v4_once = (ROOT / "scripts" / "paper_v4_once.sh").read_text(encoding="utf-8")
        v4_loop = (ROOT / "scripts" / "paper_v4_loop.sh").read_text(encoding="utf-8")
        v5_loop = (ROOT / "scripts" / "paper_v5_loop.sh").read_text(encoding="utf-8")
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        for producer in (v4_once, v4_loop):
            self.assertIn("--max-hedges 4", producer)
        for producer in (v5_loop, smoke):
            self.assertIn("--max-hedges 10", producer)

    def test_v5_aggressive_profile_keeps_costs_and_wires_external_probabilities(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        loop = (ROOT / "scripts" / "paper_v5_loop.sh").read_text(encoding="utf-8")
        for producer in (smoke, loop):
            self.assertIn("materialize_external_paper_signals.py", producer)
            self.assertIn("--min-edge 0.0", producer)
            self.assertIn("--min-t-reversion 0.20", producer)
        self.assertIn("slippage_bps", (ROOT / "config" / "paper_v5.json").read_text(encoding="utf-8"))

    def test_v5_parent_is_fail_closed_and_all_children_are_executed(self):
        smoke = (ROOT / ".github" / "workflows" / "v4-live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("assert len(manifest['strategies']) == 5", smoke)
        self.assertIn("expected = {'micro', 'pca', 'graph', 'semantic', 'external'}", smoke)
        self.assertIn("active = {name: weight for name, weight in child['expert_weights'].items() if weight > 0}", smoke)
        self.assertIn("assert active == {item['expert']: 1.0}", smoke)
        self.assertIn("run_dir = Path(item['run_dir'])", smoke)
        self.assertIn("log = run_dir / 'engine.log'", smoke)
        self.assertIn("assert {row['name'] for row in rows} == expected", smoke)


if __name__ == "__main__":
    unittest.main()
