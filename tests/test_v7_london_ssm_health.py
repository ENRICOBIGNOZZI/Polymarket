from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))

import v7_london_ssm_health as m

SHA = "a" * 40


def selected(user="enrico", run_root="/home/enrico/polymarket-runs/paper_v7_london"):
    return {
        "instance_id": "i-abc123",
        "unit_active": True,
        "app": {"user": user, "path": f"/home/{user}/polymarket"},
        "run_root": run_root,
    }


def test_health_command_is_paper_exact_sha_and_monitoring_complete():
    command = m.health_command(SHA, selected())
    assert f"SHA={SHA}" in command
    assert "polymarket-v7-paper.service" in command
    assert "polymarket-v7-exporter.service" in command
    assert "paper_only" in command
    assert "authenticated_execution" in command
    assert "real_order_submission" in command
    assert "127.0.0.1:9108/healthz" in command
    assert "127.0.0.1:9090/-/ready" in command
    assert "127.0.0.1:3000/api/health" in command
    assert "polymarket-v7" in command


@pytest.mark.parametrize("root", ["relative/path", "/tmp/x", "/root/private"])
def test_health_command_rejects_unsafe_run_root(root):
    with pytest.raises(m.SsmDeployError, match="unsafe run root"):
        m.health_command(SHA, selected(run_root=root))


def test_health_command_allows_ubuntu_data_volume_default():
    s = selected(user="ubuntu", run_root=None)
    command = m.health_command(SHA, s)
    assert "/mnt/polymarket-data/paper_v7_london" in command


def test_health_command_rejects_wrong_repo_identity():
    s = selected()
    s["app"]["path"] = "/tmp/polymarket"
    with pytest.raises(m.SsmDeployError, match="repository identity"):
        m.health_command(SHA, s)
