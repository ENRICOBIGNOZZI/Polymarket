#include "pm/v7_external_state.hpp"
#include "pm/v7_external_ws.hpp"

#include <boost/json.hpp>

#include <atomic>
#include <chrono>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

namespace fs = std::filesystem;
namespace json = boost::json;
using namespace pm::v7::external_fair;

namespace {
std::atomic<bool> stopping{false};

void signal_handler(int) noexcept { stopping.store(true, std::memory_order_relaxed); }

std::int64_t monotonic_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::int64_t wall_now_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

void atomic_write(const fs::path& path, const json::object& value) {
    fs::create_directories(path.parent_path());
    const auto temporary = path.string() + ".tmp." + std::to_string(::getpid());
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        if (!output) throw std::runtime_error("cannot open external venue status temporary");
        output << json::serialize(value) << '\n';
        output.flush();
        if (!output) throw std::runtime_error("cannot write external venue status");
    }
    fs::rename(temporary, path);
}

json::object transport_json(const ExternalWsSnapshot& value, const char* venue) {
    return {
        {"venue", venue},
        {"connection_epoch", value.connection_epoch},
        {"connection_attempts", value.connection_attempts},
        {"successful_connections", value.successful_connections},
        {"frames_received", value.frames_received},
        {"transport_failures", value.transport_failures},
        {"decode_failures", value.decode_failures},
        {"last_receive_monotonic_ns", value.last_receive_monotonic_ns},
        {"last_receive_wall_ns", value.last_receive_wall_ns},
        {"connected", value.connected != 0},
        {"healthy", value.healthy != 0},
    };
}
} // namespace

int main(int argc, char** argv) {
    try {
        fs::path output;
        std::string model_sha;
        for (int index = 1; index < argc; ++index) {
            const std::string argument = argv[index];
            if (argument == "--output" && index + 1 < argc) output = argv[++index];
            else if (argument == "--model-sha" && index + 1 < argc) model_sha = argv[++index];
            else throw std::invalid_argument("unknown or incomplete argument: " + argument);
        }
        if (output.empty() || model_sha.size() != 40) {
            throw std::invalid_argument("--output and exact --model-sha are required");
        }
        std::signal(SIGINT, signal_handler);
        std::signal(SIGTERM, signal_handler);

        constexpr std::uint64_t asset_handle = 0x425443555344ULL; // BTCUSD
        ExternalStatePolicy policy;
        ExternalAssetState state(asset_handle);
        ExternalVenueIngress binance_ingress(VenueId::BinanceSpot, asset_handle);
        ExternalVenueIngress coinbase_ingress(VenueId::CoinbaseSpot, asset_handle);
        ExternalVenueIngress bybit_ingress(VenueId::BybitSpot, asset_handle);
        ExternalVenueWsClient binance(btc_spot_connection_spec(VenueId::BinanceSpot, asset_handle), binance_ingress);
        ExternalVenueWsClient coinbase(btc_spot_connection_spec(VenueId::CoinbaseSpot, asset_handle), coinbase_ingress);
        ExternalVenueWsClient bybit(btc_spot_connection_spec(VenueId::BybitSpot, asset_handle), bybit_ingress);

#if defined(__APPLE__)
        std::thread binance_thread([&] { binance.run(ExternalStopToken(stopping)); });
        std::thread coinbase_thread([&] { coinbase.run(ExternalStopToken(stopping)); });
        std::thread bybit_thread([&] { bybit.run(ExternalStopToken(stopping)); });
#else
        std::jthread binance_thread([&](std::stop_token token) { binance.run(token); });
        std::jthread coinbase_thread([&](std::stop_token token) { coinbase.run(token); });
        std::jthread bybit_thread([&](std::stop_token token) { bybit.run(token); });
#endif

        while (!stopping.load(std::memory_order_relaxed)) {
            const auto drained = binance_ingress.drain_into(state, policy)
                + coinbase_ingress.drain_into(state, policy)
                + bybit_ingress.drain_into(state, policy);
            const auto now_mono = monotonic_now_ns();
            const auto snapshot = state.snapshot(now_mono, policy);
            const auto binance_status = binance.snapshot();
            const auto coinbase_status = coinbase.snapshot();
            const auto bybit_status = bybit.snapshot();
            json::array venues;
            venues.emplace_back(transport_json(binance_status, "BINANCE_SPOT"));
            venues.emplace_back(transport_json(coinbase_status, "COINBASE_SPOT"));
            venues.emplace_back(transport_json(bybit_status, "BYBIT_SPOT"));
            atomic_write(output, {
                {"schema", "polymarket_v7_external_venue_runtime_v1"},
                {"timestamp_ns", wall_now_ns()},
                {"code_sha", model_sha},
                {"paper_only", true},
                {"authenticated_execution", false},
                {"real_order_submission", false},
                {"state", snapshot.valid != 0 ? "OPERATIONAL" : "WARMING_OR_DEGRADED"},
                {"valid", snapshot.valid != 0},
                {"state_version", snapshot.state_version},
                {"composite_price", snapshot.venue_composite_price},
                {"composite_microprice", snapshot.venue_composite_microprice},
                {"dispersion_bps", snapshot.venue_dispersion_bps},
                {"fresh_venue_count", snapshot.venue_count_fresh},
                {"venue_health_mask", snapshot.venue_health_mask},
                {"return_250ms", snapshot.venue_composite_return_250ms},
                {"return_1s", snapshot.venue_composite_return_1s},
                {"return_5s", snapshot.venue_composite_return_5s},
                {"realized_vol_fast", snapshot.realized_vol_fast},
                {"realized_vol_medium", snapshot.realized_vol_medium},
                {"realized_vol_slow", snapshot.realized_vol_slow},
                {"aggregate_ofi", snapshot.aggregate_ofi},
                {"aggregate_trade_imbalance", snapshot.aggregate_trade_imbalance},
                {"latest_input_receive_monotonic_ns", snapshot.latest_input_receive_monotonic_ns},
                {"drained_last_cycle", drained},
                {"venues", std::move(venues)},
            });
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

#if !defined(__APPLE__)
        binance_thread.request_stop();
        coinbase_thread.request_stop();
        bybit_thread.request_stop();
#endif
        if (binance_thread.joinable()) binance_thread.join();
        if (coinbase_thread.joinable()) coinbase_thread.join();
        if (bybit_thread.joinable()) bybit_thread.join();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "v7_external_venue_runtime: " << error.what() << '\n';
        return 2;
    }
}
