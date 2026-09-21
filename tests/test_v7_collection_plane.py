from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_collection_plane_is_separate_from_trading_model_lifecycle():
    script = (ROOT / "scripts/v7_collection_plane.sh").read_text()
    assert "PM_V7_COLLECTION_ROOT" in script
    assert "/mnt/polymarket-data/polymarket_v7_collection" in script
    assert "PM_V7_COLLECTION_SHA" in script
    assert "model_independent" in script
    assert "ZERO_AUTHORITY_DATA_COLLECTION" in script
    assert "PM_V7_AUTHENTICATED_EXECUTION=1" not in script
    assert "PM_V7_REAL_ORDER_SUBMISSION=1" not in script


def test_collection_service_has_its_own_systemd_lifecycle_and_root():
    unit = (
        ROOT / "ops/systemd/polymarket-v7-collection.service.in"
    ).read_text()
    assert "ExecStart=@APP_DIR@/scripts/v7_collection_plane.sh" in unit
    assert "PM_V7_COLLECTION_ROOT=@COLLECTION_ROOT@" in unit
    assert "PM_V7_COLLECTION_SHA=@EXPECTED_SHA@" in unit
    assert "PM_V7_AUTHENTICATED_EXECUTION=0" in unit
    assert "PM_V7_REAL_ORDER_SUBMISSION=0" in unit
    assert "ReadWritePaths=@COLLECTION_ROOT@" in unit
    assert "polymarket-v7-paper.service" not in unit


def test_collection_ssm_transport_cannot_be_confused_with_model_deploy():
    source = (ROOT / "ops/v7_collection_plane_ssm.py").read_text()
    assert 'COMMENT = "Polymarket V7 model-independent collection plane"' in source
    assert "Polymarket V7 exact-SHA PAPER SSM transport" not in source
    assert 'PAPER_UNIT=polymarket-v7-paper.service' in source
    assert 'systemctl show "$PAPER_UNIT"' in source
    assert 'systemctl is-active "$PAPER_UNIT"' in source
    assert 'systemctl stop "$PAPER_UNIT"' not in source
    assert 'systemctl restart "$PAPER_UNIT"' not in source
    assert 'systemctl start "$PAPER_UNIT"' not in source


def test_collection_installer_requires_observed_tape_growth():
    source = (ROOT / "ops/v7_collection_plane_ssm.py").read_text()
    assert '"growth_bytes_10s"' in source
    assert '"paper_service_pid_unchanged":True' in source
    assert '"model_independent":True' in source
    assert '"execution_authority":"ZERO_AUTHORITY_DATA_COLLECTION"' in source


def test_collection_workflow_is_request_scoped_and_zero_authority():
    workflow = (ROOT / ".github/workflows/v7-collection-plane.yml").read_text()
    assert "deploy/v7-collection-plane-request.json" in workflow
    assert "INSTALL_OR_REFRESH_COLLECTION_ONLY" in workflow
    assert "ZERO_AUTHORITY_DATA_COLLECTION" in workflow
    assert "paper_service_pid_unchanged" in workflow
    assert "growth_bytes_10s" in workflow
