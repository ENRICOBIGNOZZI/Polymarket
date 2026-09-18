from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "v7_crypto_settlement_native_candidate.cpp"
CMAKE = ROOT / "CMakeLists.txt"
MANIFEST = ROOT / "config" / "v7_process_manifest.json"


def test_candidate_is_native_and_not_deployed_early() -> None:
    text = SOURCE.read_text()
    assert "NativeCryptoDecisionLane" in text
    assert "NativeOrderTxOwner" in text
    assert "MarketWebSocketFeed" in text
    assert "ExternalVenueWsClient" in text
    assert "prepare_submit" in text
    assert "real_order_submission\", false" in text
    assert "No network order adapter is instantiated" in text
    assert "polymarket_v7_crypto_settlement_native_candidate" in CMAKE.read_text()
    # Deployment remains fail-closed until maker/component semantics converge.
    assert "crypto_settlement_native_candidate" not in MANIFEST.read_text()


def test_no_forbidden_hot_path_surfaces_in_candidate() -> None:
    text = SOURCE.read_text()
    for forbidden in ("std::filesystem", "fstream", "curl", "requests", "sqlite", "system(", "popen("):
        assert forbidden not in text
