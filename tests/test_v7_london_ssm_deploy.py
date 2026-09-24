from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops"))

import v7_london_ssm_deploy as m


SHA = "a" * 40


def probe(instance, *, ip=None, active=False, user="enrico", run_root=None):
    return {
        "instance_id": instance,
        "unit_active": active,
        "unit_user": user,
        "app": {"user": user, "path": f"/home/{user}/polymarket"},
        "app_candidates": [{"user": user, "path": f"/home/{user}/polymarket"}],
        "repo_sha": "b" * 40,
        "repo_dirty": False,
        "tailscale_ips": [ip] if ip else [],
        "run_root": run_root,
        "runtime_state": "running" if active else None,
        "runtime_sha": "b" * 40 if active else None,
        "paper_only": True if active else None,
        "authenticated_execution": False if active else None,
        "real_order_submission": False if active else None,
    }


def test_select_target_prefers_exact_instance_id():
    probes = {
        "i-1": probe("i-1", active=False),
        "i-2": probe("i-2", active=False),
        "i-3": probe("i-3", active=False),
    }
    selected = m.select_target(probes, "", "i-2")
    assert selected["instance_id"] == "i-2"
    assert selected["selection_reason"] == "EXACT_INSTANCE_ID"


def test_select_target_rejects_missing_exact_instance():
    probes = {"i-1": probe("i-1", active=False)}
    with pytest.raises(m.SsmDeployError, match="expected London instance unavailable"):
        m.select_target(probes, "", "i-2")


def test_select_target_prefers_exact_tailscale_ip():
    probes = {
        "i-1": probe("i-1", active=True),
        "i-2": probe("i-2", ip="100.104.183.109", active=False),
    }
    selected = m.select_target(probes, "100.104.183.109")
    assert selected["instance_id"] == "i-2"
    assert selected["selection_reason"] == "EXACT_TAILSCALE_IP"


def test_select_target_falls_back_only_to_unique_active_service():
    probes = {
        "i-1": probe("i-1", active=False),
        "i-2": probe("i-2", active=True),
        "i-3": probe("i-3", active=False),
    }
    selected = m.select_target(probes, "100.104.183.109")
    assert selected["instance_id"] == "i-2"
    assert selected["selection_reason"] == "UNIQUE_ACTIVE_PAPER_SERVICE"


@pytest.mark.parametrize("probes", [
    {"i-1": probe("i-1"), "i-2": probe("i-2")},
    {"i-1": probe("i-1", active=True), "i-2": probe("i-2", active=True)},
    {"i-1": probe("i-1", ip="100.1.1.1"), "i-2": probe("i-2", ip="100.1.1.1")},
])
def test_select_target_ambiguous_or_missing_fails(probes):
    ip = "100.1.1.1" if all("100.1.1.1" in x["tailscale_ips"] for x in probes.values()) else "100.9.9.9"
    with pytest.raises(m.SsmDeployError):
        m.select_target(probes, ip)


def test_select_target_rejects_unexpected_app_path():
    p = probe("i-1", active=True)
    p["app"]["path"] = "/tmp/polymarket"
    with pytest.raises(m.SsmDeployError, match="repository path"):
        m.select_target({"i-1": p}, "")


def test_cutover_command_reuses_canonical_stage_and_cutover():
    selected = probe(
        "i-1", active=True, user="enrico",
        run_root="/home/enrico/polymarket-runs/paper_v7_london",
    )
    selected["selection_reason"] = "UNIQUE_ACTIVE_PAPER_SERVICE"
    command = m.cutover_command(SHA, selected)
    assert "ops/v7_london_stage_release.sh" in command
    assert "ops/v7_london_cutover.sh" in command
    assert f"SHA={SHA}" in command
    assert "POLYMARKET_SERVICE_USER" in command
    assert "PM_V7_RUN_ROOT" in command
    assert "paper_only" in command
    assert "authenticated_execution" in command
    assert "real_order_submission" in command
    assert "worktree add --detach" in command
    assert 'rm -rf -- "$RUNTIME_ROOT/by-sha/$SHA"' not in command
    assert "fetch --no-tags --prune origin \'+refs/heads/*:refs/remotes/origin/*\'" in command
    assert "fetch --no-tags origin main" not in command
    assert "apt-get install -y python3-numpy" not in command
    assert 'POLYMARKET_REUSE_EXACT_SHA_CI="$REUSE_EXACT_SHA_CI"' in command
    assert 'STAGE_TMPDIR=/var/tmp/pmv7-' in command
    assert 'sudo -u "$SERVICE_USER" -H env TMPDIR="$STAGE_TMPDIR"' in command
    stage = command.index("ops/v7_london_stage_release.sh")
    cutover = command.index("ops/v7_london_cutover.sh")
    assert stage < cutover


def test_cutover_command_ubuntu_uses_data_volume_default():
    selected = probe("i-1", active=True, user="ubuntu", run_root=None)
    selected["selection_reason"] = "UNIQUE_ACTIVE_PAPER_SERVICE"
    command = m.cutover_command(SHA, selected)
    assert "/mnt/polymarket-data/paper_v7_london" in command
    assert "/home/ubuntu/polymarket-artifacts" in command
    assert "/home/ubuntu/.cache/polymarket-v7-deploy/" in command
    assert "/tmp/polymarket-v7-deploy-" not in command
    assert 'rm -rf -- "$WORKTREE"' in command


@pytest.mark.parametrize("run_root", ["/tmp/x", "relative/path", "/root/private"])
def test_cutover_command_rejects_unsafe_run_root(run_root):
    selected = probe("i-1", active=True, user="enrico", run_root=run_root)
    with pytest.raises(m.SsmDeployError, match="unsafe run root"):
        m.cutover_command(SHA, selected)


def test_same_sha_live_requires_fresh_running_exact_generation():
    p = probe("i-1", active=True)
    p.update({
        "runtime_state": "running",
        "runtime_sha": SHA,
        "release_sha": SHA,
        "runtime_pid": 123,
        "runtime_pid_alive": True,
        "runtime_fresh": True,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    })
    assert m.same_sha_live(p, SHA) is True
    for key, bad in (
        ("runtime_state", "stopping"),
        ("runtime_sha", "b" * 40),
        ("release_sha", "b" * 40),
        ("runtime_pid_alive", False),
        ("runtime_fresh", False),
        ("unit_active", False),
    ):
        q = dict(p)
        q[key] = bad
        assert m.same_sha_live(q, SHA) is False


def test_artifact_upload_never_overwrites_existing_exact_sha_generation():
    source = (ROOT / "ops/v7_london_ssm_deploy.py").read_text()
    assert 'REUSED=0' in source
    assert 'if [[ -e "$TARGET" || -L "$TARGET" ]]' in source
    assert 'printf \'V7_ARTIFACT_REUSED=%s\\n\' "$REUSED"' in source
    assert 'rm -rf "$TARGET"' not in source
    assert "existing exact-SHA artifact target is unsafe" in source


def test_artifact_validation_hashes_and_bounds(tmp_path):
    p = tmp_path / "artifact.tgz"
    p.write_bytes(b"abc")
    size, digest = m.validate_artifact(p, SHA)
    assert size == 3
    assert digest == hashlib.sha256(b"abc").hexdigest()


def test_artifact_validation_rejects_symlink(tmp_path):
    p = tmp_path / "artifact.tgz"
    p.write_bytes(b"abc")
    link = tmp_path / "link.tgz"
    link.symlink_to(p)
    with pytest.raises(m.SsmDeployError):
        m.validate_artifact(link, SHA)


def test_artifact_validation_rejects_oversize(tmp_path, monkeypatch):
    p = tmp_path / "artifact.tgz"
    p.write_bytes(b"1234")
    monkeypatch.setattr(m, "MAX_ARTIFACT_BYTES", 3)
    with pytest.raises(m.SsmDeployError, match="size"):
        m.validate_artifact(p, SHA)


def test_parse_marker_requires_exactly_one():
    assert m.parse_marker('x\nV7_SSM_CUTOVER={"sha":"x"}\n', "V7_SSM_CUTOVER=") == {"sha": "x"}
    with pytest.raises(m.SsmDeployError):
        m.parse_marker("x\n", "V7_SSM_CUTOVER=")
    with pytest.raises(m.SsmDeployError):
        m.parse_marker("V7_SSM_CUTOVER={}\nV7_SSM_CUTOVER={}\n", "V7_SSM_CUTOVER=")


def test_probe_command_is_read_only():
    command = m.PROBE_COMMAND
    for forbidden in ("systemctl stop", "systemctl start", "systemctl restart", "git fetch", "rm -rf", "mv "):
        assert forbidden not in command
    assert "systemctl','is-active" in command
    assert "tailscale','ip','-4" in command

def test_transport_target_sha_extracts_exact_cutover_sha():
    old = "b" * 40
    command = {
        "Parameters": {
            "commands": [
                f"bash -lc 'set -euo pipefail; SHA={old}; echo x'"
            ],
        },
    }
    assert m._transport_target_sha(command) == old
    assert m._transport_target_sha({"Parameters": {"commands": ["echo no-sha"]}}) is None


def test_transport_target_sha_accepts_multiline_shell_assignment():
    old = "b" * 40
    command = {
        "Parameters": {
            "commands": [
                "bash -lc 'set -euo pipefail\n"
                f"SHA={old}\n"
                "echo deploy\n'"
            ],
        },
    }
    assert m._transport_target_sha(command) == old


def test_transport_target_sha_does_not_match_embedded_variable_name():
    old = "b" * 40
    command = {
        "Parameters": {
            "commands": [f"bash -lc 'NOT_SHA={old}\necho x'"],
        },
    }
    assert m._transport_target_sha(command) is None


def test_cancel_prior_deploy_transports_cancels_only_proven_older_sha(monkeypatch):
    old = "b" * 40
    cancelled = []

    def fake_aws(region, args):
        if args[:2] == ["ssm", "list-commands"]:
            return {
                "Commands": [
                    {
                        "Comment": m.DEPLOY_COMMENT,
                        "Status": "InProgress",
                        "CommandId": "cmd-old",
                        "Parameters": {
                            "commands": [f"bash -lc 'SHA={old}; echo V7_SSM_CUTOVER=old'"],
                        },
                    },
                    {
                        "Comment": "unrelated",
                        "Status": "InProgress",
                        "CommandId": "cmd-other",
                        "Parameters": {"commands": [f"SHA={old}"]},
                    },
                ],
            }
        if args[:2] == ["ssm", "cancel-command"]:
            cancelled.append(tuple(args))
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(m, "aws_json", fake_aws)
    monkeypatch.setattr(
        m, "wait",
        lambda region, instance, command_id, timeout_s, poll_s: {"Status": "Cancelled"},
    )
    recovered = m.cancel_prior_deploy_transports("eu-west-2", "i-123abc", SHA)
    assert recovered == [{
        "command_id": "cmd-old",
        "target_sha": old,
        "final_status": "Cancelled",
    }]
    assert len(cancelled) == 1
    assert "cmd-old" in cancelled[0]


def test_cancel_prior_deploy_transports_refuses_same_sha(monkeypatch):
    def fake_aws(region, args):
        assert args[:2] == ["ssm", "list-commands"]
        return {
            "Commands": [{
                "Comment": m.DEPLOY_COMMENT,
                "Status": "InProgress",
                "CommandId": "cmd-current",
                "Parameters": {
                    "commands": [f"bash -lc 'SHA={SHA}; echo V7_SSM_CUTOVER=current'"],
                },
            }],
        }

    monkeypatch.setattr(m, "aws_json", fake_aws)
    with pytest.raises(m.SsmDeployError, match="same-SHA"):
        m.cancel_prior_deploy_transports("eu-west-2", "i-123abc", SHA)


def test_cancel_prior_deploy_transports_ignores_auxiliary_research_job(monkeypatch):
    old = "b" * 40
    cancelled = []

    def fake_aws(region, args):
        if args[:2] == ["ssm", "list-commands"]:
            return {
                "Commands": [{
                    "Comment": m.DEPLOY_COMMENT,
                    "Status": "InProgress",
                    "CommandId": "cmd-research",
                    "Parameters": {
                        "commands": [
                            f"bash -lc 'PYTHONPATH=/tmp/research:/home/ubuntu/runtime/{old} "
                            "python -m research.walk_forward_v3.all_crypto_compact_equity'"
                        ],
                    },
                }],
            }
        if args[:2] == ["ssm", "cancel-command"]:
            cancelled.append(args)
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(m, "aws_json", fake_aws)
    assert m.cancel_prior_deploy_transports("eu-west-2", "i-123abc", SHA) == []
    assert cancelled == []


def test_run_uses_auxiliary_comment_by_default(monkeypatch):
    seen = {}

    def fake_send(region, instance, command, timeout_s, comment):
        seen["comment"] = comment
        return "cmd"

    monkeypatch.setattr(m, "send", fake_send)
    monkeypatch.setattr(m, "wait", lambda *args, **kwargs: {
        "Status": "Success", "StandardOutputContent": "", "StandardErrorContent": "",
    })
    m.run("eu-west-2", "i-123abc", "echo x", 10)
    assert seen["comment"] == m.GENERIC_SSM_COMMENT


def test_main_writes_receipt_as_one_valid_json_document(tmp_path, monkeypatch):
    out = tmp_path / "receipt.json"
    artifact = tmp_path / "artifact.tgz"
    artifact.write_bytes(b"x")
    receipt = {
        "schema": "polymarket_v7_ssm_deploy_receipt_v1",
        "expected_sha": SHA,
        "selected": {
            "instance_id": "i-123abc",
            "selection_reason": "EXACT_INSTANCE_ID",
        },
    }
    monkeypatch.setattr(m, "deploy", lambda *args, **kwargs: receipt)
    monkeypatch.setattr(sys, "argv", [
        "v7_london_ssm_deploy.py",
        "--expected-sha", SHA,
        "--artifact", str(artifact),
        "--output", str(out),
    ])
    assert m.main() == 0
    import json
    assert json.loads(out.read_text(encoding="utf-8")) == receipt
    assert out.read_text(encoding="utf-8").endswith("\n")
