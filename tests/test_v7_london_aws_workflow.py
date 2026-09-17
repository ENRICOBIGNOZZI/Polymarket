from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/v7-london-aws-provision.yml"


def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_is_manual_exact_sha_and_paper_only() -> None:
    s = text()
    assert "name: V7 London AWS provision and benchmark" in s
    assert "workflow_dispatch:" in s
    assert "expected_sha:" in s
    assert "infrastructure_approved:" in s
    assert "python3 scripts/v7_cutover_contract.py" in s
    assert "authenticated_execution'] is False" in s
    assert "real_order_submission'] is False" in s
    assert "automatic_cutover'] is False" in s


def test_workflow_uses_aws_identity_without_tailscale() -> None:
    s = text()
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert f"secrets.{name}" in s
    assert "aws sts get-caller-identity" in s
    assert "TS_AUTHKEY" not in s
    assert "tailscale/github-action" not in s


def test_provision_bootstrap_smoke_is_ssm_only_and_no_cutover() -> None:
    s = text()
    for required in (
        "ops/v7_london_provision.sh",
        "ops/v7_london_ssm_bootstrap.py",
        "ops/v7_london_ssm_benchmark.py smoke",
        "ops/v7_london_multipath_ssm.py",
    ):
        assert required in s
    assert "v7_london_cutover.sh" not in s
    assert "systemctl enable --now polymarket-v7-paper" not in s


def test_formal_benchmark_is_detached_and_collectable_later() -> None:
    s = text()
    assert "launch-formal" in s and "collect-formal" in s
    assert "ops/v7_london_ssm_benchmark.py formal" in s
    assert "v7-london-formal-receipt" in s
    assert 'gh run download "$FORMAL_RUN_ID"' in s
    assert "ops/v7_london_ssm_benchmark.py collect" in s
    assert "formal.$EXPECTED_SHA.shootout.json" in s


if __name__ == "__main__":
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    assert tests
    for _, function in tests:
        function()
    print(f"{len(tests)} function tests passed")
