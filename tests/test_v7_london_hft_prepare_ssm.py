import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_london_hft_prepare_ssm", ROOT / "ops/v7_london_hft_prepare_ssm.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

SHA = "1" * 40


def test_prepare_command_is_paper_only_and_requires_stopped_runtime():
    command = MODULE.prepare_command(SHA, "ubuntu", "euw2-az1")
    assert "! systemctl is-active --quiet polymarket-v7-paper.service" in command
    assert "v7_runtime_resource_plan.py" in command
    assert "v7_linux_hft_prepare.py" in command
    assert "--apply" in command
    assert "authenticated_execution" in command
    assert "real_order_submission" in command


def test_audit_command_separates_baseline_from_network_gate():
    command = MODULE.audit_command(SHA, "ubuntu", "euw2-az2")
    assert "v7_linux_hft_host_audit.py" in command
    assert "baseline_ready" in command
    assert "Network readiness is intentionally a separate ENA A/B gate" in command


def test_parse_envelope_requires_exactly_one_object():
    value = MODULE.parse_envelope('noise\nV7_HFT_PREP={"paper_only":true}\n', "V7_HFT_PREP=")
    assert value["paper_only"] is True
