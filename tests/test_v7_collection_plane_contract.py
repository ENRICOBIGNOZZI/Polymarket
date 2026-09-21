from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_collection_plane_is_zero_authority_and_model_independent():
    script = (ROOT / "scripts/v7_collection_plane.sh").read_text()
    assert 'COLLECTION_ROOT="${PM_V7_COLLECTION_ROOT:-/mnt/polymarket-data/polymarket_v7_collection}"' in script
    assert '"model_independent":True' in script
    assert '"live_model_required":False' in script
    assert '"execution_authority":"ZERO_AUTHORITY_DATA_COLLECTION"' in script
    assert '"authenticated_execution":False' in script
    assert '"real_order_submission":False' in script
    assert '"real_capital_at_risk":False' in script
    assert "ledger_spool" not in script
    assert "authority.submit" not in script


def test_collection_systemd_has_independent_lifecycle_and_root():
    unit = (
        ROOT / "ops/systemd/polymarket-v7-collection.service.in"
    ).read_text()
    assert "Restart=always" in unit
    assert "PM_V7_COLLECTION_ROOT=@COLLECTION_ROOT@" in unit
    assert "PM_V7_AUTHENTICATED_EXECUTION=0" in unit
    assert "PM_V7_REAL_ORDER_SUBMISSION=0" in unit
    assert "polymarket-v7-paper.service" not in unit
    assert "EnvironmentFile=" not in unit


def test_collection_only_deployer_never_controls_trading_runtime():
    source = (
        ROOT / "ops/v7_collection_plane_ssm_deploy.py"
    ).read_text()
    forbidden = (
        "systemctl stop polymarket-v7-paper.service",
        "systemctl restart polymarket-v7-paper.service",
        "systemctl start polymarket-v7-paper.service",
        "systemctl enable polymarket-v7-paper.service",
        "ops/v7_london_cutover.sh",
    )
    for token in forbidden:
        assert token not in source
    assert "polymarket-v7-collection.service" in source
    assert "polymarket-v7-collection-retention.timer" in source
    assert "observed_ingress_gb_per_hour" in source
    assert "fresh_external_feeds" in source


def test_collection_deploy_workflow_requires_explicit_zero_authority_request():
    workflow = (
        ROOT / ".github/workflows/v7-independent-collection-deploy.yml"
    ).read_text()
    assert "deploy/v7-collection-plane-deploy-request.json" in workflow
    assert "DEPLOY_COLLECTION_ONLY" in workflow
    assert "real_capital_at_risk" in workflow
    assert "v7_collection_plane_ssm_deploy.py" in workflow


if __name__ == "__main__":
    test_collection_plane_is_zero_authority_and_model_independent()
    test_collection_systemd_has_independent_lifecycle_and_root()
    test_collection_only_deployer_never_controls_trading_runtime()
    test_collection_deploy_workflow_requires_explicit_zero_authority_request()
