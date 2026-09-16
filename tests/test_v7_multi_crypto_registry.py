import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_multi_crypto_registry", ROOT / "scripts/v7_multi_crypto_registry.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _load():
    assets = MODULE.validate_asset_registry(
        json.loads((ROOT / "config/v7_multi_crypto_assets.json").read_text())
    )
    settlement = json.loads(
        (ROOT / "config/v7_crypto_settlement_markets.json").read_text()
    )
    return assets, settlement


def test_registry_is_six_asset_fail_closed_partition():
    assets, _ = _load()
    assert [row["asset"] for row in assets["assets"]] == [
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"
    ]
    assert assets["paper_only"] is True
    assert assets["authenticated_execution"] is False
    assert assets["real_order_submission"] is False
    assert assets["real_capital_at_risk"] is False
    assert assets["automatic_promotion"] is False
    for asset in assets["assets"]:
        assert asset["allow_new_entry_authority"] is False
        assert list(asset["horizons"]) == ["M5", "M15"]
        assert all(cfg["allow_entry_authority"] is False for cfg in asset["horizons"].values())


def test_asset_handle_preserves_frozen_btc_literal():
    assert MODULE.asset_handle("BTC") == 0x425443555344
    assert MODULE.asset_handle("ETH") != MODULE.asset_handle("BTC")
    assert MODULE.asset_handle("SOL") != MODULE.asset_handle("ETH")


def test_capability_matrix_is_shadow_only_and_fail_closed():
    assets, settlement = _load()
    rows = {(r.asset, r.horizon): r for r in MODULE.capability_rows(assets, settlement)}
    assert len(rows) == 12
    assert rows[("ETH", "M5")].status == "READY_SHADOW"
    assert rows[("SOL", "M5")].status == "READY_SHADOW"
    assert rows[("BTC", "M5")].status == "BLOCKED"
    assert "BTC_FROZEN_NO_NEW_AUTHORITY" in rows[("BTC", "M5")].blockers
    assert rows[("DOGE", "M5")].status == "BLOCKED"
    assert "SETTLEMENT_CONTEXT_UNVERIFIED_OR_MISSING" in rows[("DOGE", "M5")].blockers
    assert rows[("BNB", "M15")].status == "BLOCKED"
    assert all(r.entry_authority is False for r in rows.values())


def test_matrix_writer_emits_twelve_rows():
    assets, settlement = _load()
    rows = MODULE.capability_rows(assets, settlement)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "matrix.csv"
        MODULE.write_matrix(out, rows)
        lines = out.read_text().splitlines()
        assert len(lines) == 13
        assert lines[0].startswith("asset,horizon,")


def test_external_ws_factory_is_generic_without_asset_copies():
    header = (ROOT / "include/pm/v7_external_ws.hpp").read_text()
    source = (ROOT / "src/v7_external_ws.cpp").read_text()
    assert "crypto_connection_spec(" in header
    assert "crypto_connection_spec(" in source
    for prefix in ("eth_", "sol_", "xrp_", "doge_", "bnb_"):
        assert f"{prefix}spot_connection_spec" not in source


if __name__ == "__main__":
    tests = sorted(
        (name, fn) for name, fn in list(globals().items())
        if name.startswith("test_") and callable(fn)
    )
    assert tests
    for name, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
