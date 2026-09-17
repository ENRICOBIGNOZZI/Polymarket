import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))
SPEC = importlib.util.spec_from_file_location(
    "v7_london_ena_host_ab_ssm", ROOT / "ops/v7_london_ena_host_ab_ssm.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
SHA = "2" * 40


def test_remote_command_requires_baseline_and_stopped_paper():
    command = MODULE.remote_command(SHA, "euw2-az1", "ubuntu", 20, 100)
    assert "! systemctl is-active --quiet polymarket-v7-paper.service" in command
    assert "v7_linux_hft_host_audit.py" in command
    assert "baseline_ready" in command
    assert "v7_ena_host_ab.py" in command
    assert "--resource-plan" in command


def test_remote_command_does_not_enable_busy_poll_or_runtime():
    command = MODULE.remote_command(SHA, "euw2-az2", "ubuntu", 20, 100)
    assert "socket-busy-poll" not in command
    assert "systemctl start polymarket-v7-paper" not in command


def test_parse_result_requires_rollback():
    value = {
        "restored_exactly": True, "paper_only": True,
        "exact_code_sha": SHA, "region": "euw2-az1"}
    parsed = MODULE.parse_result("V7_ENA_HOST_AB=" + json.dumps(value))
    assert parsed["restored_exactly"] is True
