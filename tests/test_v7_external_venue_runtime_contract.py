#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "src" / "v7_external_venue_runtime.cpp").read_text()
    cmake = (ROOT / "CMakeLists.txt").read_text()
    launcher = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    websocket = (ROOT / "src" / "v7_external_ws.cpp").read_text()
    multi_asset = (ROOT / "scripts" / "v7_multi_asset_external_collector.py").read_text()
    assert "ExternalVenueWsClient binance" in source
    assert "std::unique_ptr<ExternalVenueWsClient> coinbase;" in source
    assert "std::unique_ptr<ExternalVenueWsClient> bybit;" in source
    assert 'binance_usdm_market_spec.target = "/market/ws"' in source
    assert "binance_usdm_market_ingress" in source
    assert '"binance_usdm_market"' in source
    assert "ExternalAssetState state" in source
    assert '"real_order_submission", false' in source
    assert "polymarket_v7_external_venue_runtime" in cmake
    assert 'source scripts/v7_process_runtime.sh' in launcher
    assert 'v7_register_child "$!"' in launcher
    assert "v7_assert_registered_child_count 9" in launcher
    assert "external_venues.json" in launcher
    assert "LatestJsonPublisher status_publisher(output)" in source
    assert "status_publisher.publish(std::move(status_payload))" in source
    assert "atomic_write(output" not in source
    assert '"--external-cancel-signal"' in source
    assert "advance_external_cancel_signal" in source
    assert '"shock_window_ms", 100' in source
    assert '"minimum_absolute_log_return_bp", 0.30' in source
    assert '"trigger_cooldown_ms", 250' in source
    assert '"trigger_grid_ms", 25' in source
    assert "std::make_unique<beast::flat_static_buffer" in websocket
    assert "flat_static_buffer<kMaxWsMessageBytes> buffer;" not in websocket
    assert "constexpr std::size_t kMaxWsMessageBytes = 2U << 20;" in websocket
    assert 'spec.target = "/public/ws"' in websocket
    assert 'argument == "--disk-pressure-marker"' in source
    assert 'argument == "--disk-pressure-min-free-bytes"' in source
    assert "fs::space" in source
    assert '"suppressed_by_policy"' in source
    assert '--disk-pressure-marker "$RUN_ROOT/control/DISK_PRESSURE"' in launcher
    assert '--disk-pressure-min-free-bytes "$DISK_PRESSURE_MIN_FREE_BYTES"' in launcher
    assert "scripts/v7_multi_asset_external_collector.py" in launcher
    assert "--event-driven-ingress" in multi_asset
    assert 'ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")' in multi_asset
    assert "all_assets_status.json" in multi_asset
    assert '"execution_authority": False' in multi_asset
    assert 'argument == "--asset"' in source
    assert "crypto_connection_spec(" in source
    assert "non-BTC assets require Binance spot as primary feed" in source
    assert "non-BTC assets require at least two enabled spot venues" in source
    assert 'symbol == "-" || symbol == "NONE"' in source
    assert 'policy.use_transport_freshness_for_book = asset == "BTC" ? 0 : 1;' in source
    assert source.count("+ deribit_ingress.drain_into(state, policy)") == 1
    assert "BTC frozen external-cancel signal cannot be reused for non-BTC assets" in source
    assert 'ticker.BTC-PERPETUAL.' not in source
    assert 'orderbook.50.BTCUSDT' not in source
    assert 'tickers.BTCUSDT' not in source
    assert '/api/v3/depth?symbol=BTCUSDT' not in source


if __name__ == "__main__":
    main()


def test_repricing_book_observer_is_optional_child() -> None:
    launcher = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text()
    segment = launcher.split('v7_exec_class COLLECTOR "$FILLABILITY_OBSERVER"', 1)[1].split('v7_register_optional_child "$!"', 1)[0]
    assert '--selection "$RUN_ROOT/universe/book_selection.json" --selection-only' in segment
    assert 'v7_register_child "$!"' not in segment
    assert 'for pid in "${fatal_pids[@]}"' in launcher
