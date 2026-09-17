#include "pm/v7_external_protocol.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <vector>

using namespace pm::v7::external_fair;
namespace {
std::int64_t monotonic_ns() noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
}

int main(int argc, char** argv) {
    const std::size_t samples = argc > 1 ? std::strtoull(argv[1], nullptr, 10) : 200'000;
    if (samples == 0) return 64;
    constexpr std::string_view book =
        R"({"u":400900217,"s":"BTCUSDT","b":"65000.10","B":"1.20","a":"65000.20","A":"2.30"})";
    constexpr std::string_view trade =
        R"({"e":"aggTrade","E":1672515782136,"s":"BTCUSDT","a":12345,"p":"65000.50","q":"0.25","T":1672515782136,"m":false})";
    std::array<ExternalVenueEvent, 8> output{};
    for (int i = 0; i < 2'000; ++i) {
        (void)decode_external_venue_frame(VenueId::BinanceSpot, 1, 1,
            1'000 + i, 2'000 + i, (i & 1) ? book : trade, output);
    }
    std::vector<std::int64_t> latency;
    latency.reserve(samples);
    for (std::size_t i = 0; i < samples; ++i) {
        const auto start = monotonic_ns();
        const auto result = decode_external_venue_frame(VenueId::BinanceSpot, 1, 1,
            10'000 + static_cast<std::int64_t>(i), 20'000 + static_cast<std::int64_t>(i),
            (i & 1) ? book : trade, output);
        const auto finish = monotonic_ns();
        if (result.invalid_frame || result.output_count != 1) return 2;
        latency.push_back(finish - start);
    }
    std::sort(latency.begin(), latency.end());
    const auto q = [&](double p) { return latency[static_cast<std::size_t>((latency.size() - 1) * p)]; };
    std::cout << "{\"schema\":\"polymarket_v7_external_protocol_bench_v1\","
              << "\"paper_only\":true,\"authenticated_execution\":false,\"real_order_submission\":false,"
              << "\"scope\":\"BINANCE_JSON_FRAME_TO_NORMALIZED_EVENT\","
              << "\"samples\":" << samples << ",\"latency_ns\":{"
              << "\"p50\":" << q(.50) << ",\"p95\":" << q(.95)
              << ",\"p99\":" << q(.99) << ",\"p999\":" << q(.999)
              << ",\"max\":" << latency.back() << "}}\n";
}
