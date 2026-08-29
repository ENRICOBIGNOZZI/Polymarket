#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "src" / "v7_external_venue_runtime.cpp").read_text()
    cmake = (ROOT / "CMakeLists.txt").read_text()
    launcher = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    websocket = (ROOT / "src" / "v7_external_ws.cpp").read_text()
    assert "ExternalVenueWsClient binance" in source
    assert "ExternalVenueWsClient coinbase" in source
    assert "ExternalVenueWsClient bybit" in source
    assert "ExternalAssetState state" in source
    assert '"real_order_submission", false' in source
    assert "polymarket_v7_external_venue_runtime" in cmake
    assert 'pids+=("$!")' in launcher
    assert "external_venues.json" in launcher
    assert "std::make_unique<beast::flat_static_buffer" in websocket
    assert "flat_static_buffer<kMaxWsMessageBytes> buffer;" not in websocket


if __name__ == "__main__":
    main()
