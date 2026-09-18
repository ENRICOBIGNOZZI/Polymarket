from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "v7_crypto_settlement_native_candidate.cpp"
AUTHORITY = ROOT / "src" / "v7_native_settlement_authority.cpp"
CMAKE = ROOT / "CMakeLists.txt"
MANIFEST = ROOT / "config" / "v7_process_manifest.json"


def test_candidate_is_native_single_owner_and_not_deployed_early() -> None:
    text = SOURCE.read_text()
    assert "NativeCryptoDecisionLane" in text
    assert "NativeSettlementAuthority authority" in text
    assert "maker::MakerInstrumentLane" in text
    assert "MarketWebSocketFeed" in text
    assert "ExternalVenueWsClient" in text
    assert "lane.construct_candidate" in text
    assert "authority.submit" in text
    assert "arbitration_conflicts_fail_closed" in text
    assert "real_order_submission\", false" in text
    assert "network adapter remain fail-closed" in text
    assert "polymarket_v7_crypto_settlement_native_candidate" in CMAKE.read_text()
    assert "crypto_settlement_native_candidate" not in MANIFEST.read_text()

def test_candidate_cannot_bypass_shared_capital_or_oms_owner() -> None:
    text = SOURCE.read_text()
    assert "NativeOrderTxOwner" not in text
    assert "ExecutionAdmission::admit" not in text
    assert "prepare_submit" not in text
    authority = AUTHORITY.read_text()
    assert "ExecutionAdmission::admit" in authority
    assert "order_tx_.prepare_submit" in authority
    assert "InventoryUnavailable" in authority
    assert "BelowVenueMinimum" in authority


def test_no_forbidden_hot_path_surfaces_in_candidate() -> None:
    text = SOURCE.read_text()
    for forbidden in (
        "std::filesystem", "fstream", "curl", "requests", "sqlite", "system(", "popen("
    ):
        assert forbidden not in text
