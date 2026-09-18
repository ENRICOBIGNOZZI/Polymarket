#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/v7_london_az_shootout.json"
BOOTSTRAP = ROOT / "ops/v7_london_bootstrap.sh"
BENCHMARK = ROOT / "ops/v7_london_benchmark.sh"
DOC = ROOT / "docs/LONDON_MIGRATION.md"


def test_london_config_is_fail_closed_and_matches_whitelisted_zones() -> None:
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert value["schema"] == "polymarket_v7_london_az_shootout_v1"
    assert value["region"] == "eu-west-2"
    assert value["paper_only"] is True
    assert value["authenticated_execution"] is False
    assert value["real_order_submission"] is False
    assert value["automatic_cutover"] is False
    assert {row["zone_name"]: row["zone_id"] for row in value["zones"]} == {
        "eu-west-2a": "euw2-az2",
        "eu-west-2b": "euw2-az3",
        "eu-west-2c": "euw2-az1",
    }


def test_formal_shootout_spans_at_least_policy_day() -> None:
    value = json.loads(CONFIG.read_text(encoding="utf-8"))["benchmark"]
    duration_ms = (int(value["formal_samples"]) - 1) * int(value["formal_interval_ms"])
    assert duration_ms >= int(value["formal_minimum_duration_seconds"]) * 1000


def test_migration_never_reuses_live_ledger_or_run_root() -> None:
    value = json.loads(CONFIG.read_text(encoding="utf-8"))["data_migration"]
    assert value["copy_live_run_root"] is False
    assert value["copy_canonical_ledger"] is False
    assert value["new_run_generation_required"] is True


def test_bootstrap_installs_but_does_not_start_runtime() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'VERSION_ID:-}" == "24.04"' in source
    assert 'POLYMARKET_EXPECTED_SHA' in source
    assert 'checkout --detach "$EXPECTED_SHA"' in source
    assert 'v7_london_stage_release.sh' in source
    stage=(ROOT/'ops/v7_london_stage_release.sh').read_text(encoding='utf-8')
    assert 'ctest --test-dir "$SOURCE_DIR/build-verify" --output-on-failure' in stage
    assert '-DPM_LONDON_RUNTIME_ONLY=ON -DBUILD_TESTING=OFF' in stage
    assert 'build_london_runtime_bundle.py' in stage
    assert 'RUNTIME_CURRENT' in source
    assert '[[ ! -e "$RUNTIME_CURRENT/research" ]]' in source
    assert 'systemctl disable --now polymarket-v7-paper.service' in source
    assert 'tailscale up' not in source
    assert 'v7_london_install_monitoring.sh' in source
    assert 'systemctl enable --now prometheus.service prometheus-node-exporter.service grafana-server.service' in source
    assert 'systemctl disable --now polymarket-v7-paper.service polymarket-v7-exporter.service' in source
    assert 'pkg-config prometheus' not in source
    assert 'systemctl enable --now polymarket-v7-paper.service' not in source


def test_benchmark_uses_imdsv2_and_physical_az_id() -> None:
    source = BENCHMARK.read_text(encoding="utf-8")
    assert 'X-aws-ec2-metadata-token-ttl-seconds' in source
    assert 'placement/availability-zone-id' in source
    assert '--region "$AZ_ID"' in source
    assert 'polymarket_v7_latency_probe' in source


def test_shell_contracts_are_syntax_valid() -> None:
    for path in (BOOTSTRAP, BENCHMARK):
        completed = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr


def test_runbook_preserves_single_writer_and_admin_only_tailscale() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "new ledger generation" in text
    assert "one canonical writer" in text
    assert "Tailscale" in text and "admin" in text
    assert "PAPER" in text

    def test_benchmark_is_single_owner_per_host(self) -> None:
        benchmark=(ROOT / "ops/v7_london_benchmark.sh").read_text(encoding="utf-8")
        bootstrap=(ROOT / "ops/v7_london_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("POLYMARKET_BENCHMARK_LOCK_FILE", benchmark)
        self.assertIn("flock -n 9", benchmark)
        self.assertIn("another London benchmark already owns this host", benchmark)
        self.assertIn("single_owner_lock", benchmark)
        self.assertIn("util-linux", bootstrap)

if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    assert tests, "No tests collected"
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
