from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/v7-paper-server-health.yml"


def test_health_workflow_uses_current_main_monitor_against_deployed_sha():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "paths: [deploy/v7-paper-server-health-request.json]" in text
    assert "HEALTH_REQUEST_PATH: deploy/v7-paper-server-health-request.json" in text
    assert "polymarket_v7_paper_health_request_v1" in text
    assert "READ_ONLY_HEALTH" in text
    assert "git worktree add --detach" in text
    assert 'git worktree remove --force "$contract_root"' in text
    assert 'git checkout --detach "$target"' not in text
    assert 'test "$(git rev-parse HEAD)" = "$main_sha"' in text
    assert "python3 ops/v7_london_ssm_health.py" in text
    assert "health_monitoring_sha=$main_sha" in text


def test_health_request_contract_is_fail_closed_and_read_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    for required in (
        "assert set(v)=={",
        "'schema','version','request_id','expected_sha','operation'",
        "assert v['paper_only'] is True",
        "assert v['authenticated_execution'] is False",
        "assert v['real_order_submission'] is False",
        "assert v['operation']=='READ_ONLY_HEALTH'",
        "re.fullmatch(r'[0-9a-f]{40}',v['expected_sha'])",
    ):
        assert required in text


def test_target_contract_is_checked_without_downgrading_monitoring_code():
    text = WORKFLOW.read_text(encoding="utf-8")
    contract = (
        'python3 "$contract_root/scripts/v7_cutover_contract.py" '
        '\\n            --repository-root "$contract_root" '
        '--expected-head "$target" >/dev/null'
    )
    assert contract in text
    resolve = text.index("- name: Resolve deployed runtime SHA")
    monitor = text.index("- name: Verify London PAPER runtime through read-only SSM")
    assert resolve < monitor
    block = text[resolve:monitor]
    assert "git worktree add --detach" in block
    assert "git checkout --detach" not in block
