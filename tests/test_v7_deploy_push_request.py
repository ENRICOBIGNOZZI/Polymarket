from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_deploy_push_request as m


BEFORE = "a" * 40
AFTER = "b" * 40


def request(parent=BEFORE, **overrides):
    value = {
        "schema": "polymarket_v7_paper_deploy_request_v1",
        "version": 1,
        "request_id": "deploy-20260919-paper",
        "expected_parent_sha": parent,
        "cutover_approved": True,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "freeze_maker_forward_window": False,
        "trigger": "GITHUB_PUSH_ONE_SHOT",
    }
    value.update(overrides)
    return value


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_load_request_accepts_exact_one_shot(tmp_path):
    p = tmp_path / "request.json"
    write(p, request())
    value = m.load_request(p, event_before=BEFORE, event_after=AFTER)
    assert value["cutover_approved"] is True
    assert value["paper_only"] is True
    assert value["real_order_submission"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"cutover_approved": False},
        {"paper_only": False},
        {"authenticated_execution": True},
        {"real_order_submission": True},
        {"freeze_maker_forward_window": True},
        {"trigger": "MANUAL_BYPASS"},
        {"expected_parent_sha": "c" * 40},
    ],
)
def test_request_fails_closed_on_authority_or_parent_change(tmp_path, overrides):
    p = tmp_path / "request.json"
    write(p, request(**overrides))
    with pytest.raises(m.DeployRequestError):
        m.load_request(p, event_before=BEFORE, event_after=AFTER)


def test_request_rejects_unknown_fields(tmp_path):
    p = tmp_path / "request.json"
    write(p, {**request(), "extra": True})
    with pytest.raises(m.DeployRequestError, match="request_fields"):
        m.load_request(p, event_before=BEFORE, event_after=AFTER)


def _git(root: Path, *args: str):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README").write_text("base\n")
    _git(root, "add", "README")
    _git(root, "commit", "-m", "base")
    before = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return root, before


def test_validate_commit_requires_only_request_file(tmp_path):
    root, before = _init_repo(tmp_path)
    write(root / m.REQUEST_PATH, request(parent=before))
    _git(root, "add", m.REQUEST_PATH)
    _git(root, "commit", "-m", "approve deploy")
    after = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    result = m.validate_commit(root, event_before=before, event_after=after)
    assert result["valid"] is True
    assert result["expected_deploy_sha"] == after


def test_validate_commit_rejects_extra_file(tmp_path):
    root, before = _init_repo(tmp_path)
    write(root / m.REQUEST_PATH, request(parent=before))
    (root / "extra.txt").write_text("nope\n")
    _git(root, "add", m.REQUEST_PATH, "extra.txt")
    _git(root, "commit", "-m", "bad approval")
    after = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    with pytest.raises(m.DeployRequestError, match="only_change_request_file"):
        m.validate_commit(root, event_before=before, event_after=after)


def test_validate_commit_rejects_wrong_parent_binding(tmp_path):
    root, before = _init_repo(tmp_path)
    write(root / m.REQUEST_PATH, request(parent="c" * 40))
    _git(root, "add", m.REQUEST_PATH)
    _git(root, "commit", "-m", "wrong parent")
    after = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    with pytest.raises(m.DeployRequestError, match="request_parent_mismatch"):
        m.validate_commit(root, event_before=before, event_after=after)
