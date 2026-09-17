#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/v7_london_az_shootout.json"
PROVISIONER = ROOT / "scripts/v7_london_provision.py"
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


def test_provisioner_is_plan_only_by_default_and_cannot_cut_over() -> None:
    source = PROVISIONER.read_text(encoding="utf-8")
    assert 'parser.add_argument("--apply", action="store_true")' in source
    assert 'if not args.apply:' in source
    assert '"automatic_cutover": False' in source
    assert '"runtime_started": False' in source
    assert 'HttpTokens": "required"' in source


def test_bootstrap_installs_but_does_not_start_runtime() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'VERSION_ID:-}" == "24.04"' in source
    assert 'POLYMARKET_EXPECTED_SHA' in source
    assert 'checkout --detach "$EXPECTED_SHA"' in source
    assert 'ctest --test-dir "$APP_DIR/build" --output-on-failure' in source
    assert 'systemctl disable --now polymarket-v7-paper.service' in source
    assert 'tailscale up' not in source
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

if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    assert tests, "No tests collected"
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
