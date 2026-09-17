#include "pm/v7_coinbase_l2_observer.hpp"

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
    ExternalVenueIngress ingress(VenueId::CoinbaseSpot, 500, nullptr, nullptr);
    CoinbaseL2FrameObserver observer(ingress, 500);
    observer.on_connection_epoch(1);
    constexpr std::string_view snapshot = R"({"type":"snapshot","product_id":"BTC-USD","bids":[["100.00","2.0"],["99.00","4.0"],["98.00","5.0"]],"asks":[["101.00","3.0"],["102.00","5.0"],["103.00","6.0"]]})";
    constexpr std::string_view update1 = R"({"type":"l2update","product_id":"BTC-USD","changes":[["buy","100.00","2.1"],["sell","101.00","3.1"]]})";
    constexpr std::string_view update2 = R"({"type":"l2update","product_id":"BTC-USD","changes":[["buy","100.00","2.0"],["sell","101.00","3.0"]]})";
    std::array<ExternalVenueEvent, 4> events{};
    observer.on_frame(1, 1'000, 2'000, snapshot);
    (void)ingress.drain_events(events);
    for (int i = 0; i < 2'000; ++i) {
        observer.on_frame(1, 2'000 + i, 3'000 + i, (i & 1) ? update1 : update2);
        (void)ingress.drain_events(events);
    }
    std::vector<std::int64_t> latency;
    latency.reserve(samples);
    for (std::size_t i = 0; i < samples; ++i) {
        const auto start = monotonic_ns();
        observer.on_frame(1, 10'000 + static_cast<std::int64_t>(i),
                          20'000 + static_cast<std::int64_t>(i), (i & 1) ? update1 : update2);
        (void)ingress.drain_events(events);
        latency.push_back(monotonic_ns() - start);
    }
    std::sort(latency.begin(), latency.end());
    const auto q = [&](double p) { return latency[static_cast<std::size_t>((latency.size() - 1) * p)]; };
    std::cout << "{\"schema\":\"polymarket_v7_coinbase_hotpath_bench_v1\","
              << "\"paper_only\":true,\"authenticated_execution\":false,\"real_order_submission\":false,"
              << "\"scope\":\"COINBASE_JSON_FRAME_TO_NORMALIZED_INGRESS_EVENT\","
              << "\"samples\":" << samples << ",\"latency_ns\":{"
              << "\"p50\":" << q(.50) << ",\"p95\":" << q(.95)
              << ",\"p99\":" << q(.99) << ",\"p999\":" << q(.999)
              << ",\"max\":" << latency.back() << "}}\n";
}
