#include "pm/v7_crypto_decision_lane.hpp"

#include <algorithm>
#include <charconv>
#include <cstdint>
#include <iostream>
#include <string_view>
#include <vector>

using namespace pm::v7;
using pm::v7::external_fair::ExternalCancelSignalSnapshot;

namespace {
constexpr std::int64_t kNow = 2'000'000'000'000LL;

std::size_t parse_count(std::string_view text) {
    std::size_t value = 0;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size()
        || value < 100 || value > 5'000'000) return 0;
    return value;
}

BookHotSnapshot make_book(std::int32_t ask) {
    BookHotSnapshot b; b.state_version = 1; b.exchange_event_ns = kNow - 2'000'000;
    b.receive_monotonic_ns = kNow - 1'000'000; b.tick_size_e4 = 100;
    b.best_bid_e4 = ask - 100; b.best_ask_e4 = ask;
    b.best_bid_microunits = 10'000'000; b.best_ask_microunits = 10'000'000;
    b.bid_level_count = 1; b.ask_level_count = 1;
    b.bid_levels[0] = {ask - 100, 10'000'000};
    b.ask_levels[0] = {ask, 10'000'000};
    b.lineage_continuous = 1; b.valid = 1; return b;
}

NativeCryptoDecisionInput make_input(std::uint64_t version) {
    NativeCryptoDecisionInput in;
    in.signal.signal_version = version;
    in.signal.trigger_receive_monotonic_ns = kNow - 10'000'000;
    in.signal.evaluated_grid_monotonic_ns = in.signal.trigger_receive_monotonic_ns;
    in.signal.valid_until_monotonic_ns = kNow + 90'000'000;
    in.signal.binance_return_100ms_bp = 0.5;
    in.signal.coinbase_return_100ms_bp = 0.2;
    in.signal.direction = 1; in.signal.confirmed_non_opposing = 1; in.signal.valid = 1;
    in.market.market_handle = 7; in.market.event_handle = 8;
    in.market.close_monotonic_ns = kNow + 110'000'000'000LL;
    in.market.yes = {11, 1'000'000, 1, {}}; in.market.no = {12, 1'000'000, 0, {}};
    in.market.accepting_orders = 1; in.market.contract_verified = 1;
    in.market.settlement_reference_valid = 1;
    in.yes_book = make_book(4000); in.no_book = make_book(6000);
    in.now_monotonic_ns = kNow; in.model_version = 3; in.policy_version = 4;
    return in;
}
std::int64_t quantile(const std::vector<std::int64_t>& values, double q) {
    if (values.empty()) return 0;
    const auto index = static_cast<std::size_t>(q * static_cast<double>(values.size() - 1));
    return values[index];
}
}

int main(int argc, char** argv) {
    std::size_t samples = 200'000;
    if (argc == 3 && std::string_view(argv[1]) == "--samples") {
        samples = parse_count(argv[2]);
        if (samples == 0) return 64;
    } else if (argc != 1) return 64;

    NativeCryptoDecisionLane lane({});
    SleeveCapitalAccount capital(CapitalLimits{
        10'000'000, 10'000'000, 10'000'000, 5'000'000});
    for (std::uint64_t i = 1; i <= 10'000; ++i) {
        const auto result = lane.evaluate(make_input(i), capital);
        if (!result.accepted || !capital.release_order(result.intent.intent_id)) return 2;
    }
    std::vector<std::int64_t> latency; latency.reserve(samples);
    for (std::uint64_t i = 10'001; i < 10'001 + samples; ++i) {
        const auto result = lane.evaluate(make_input(i), capital);
        if (!result.accepted || !capital.release_order(result.intent.intent_id)) return 3;
        latency.push_back(result.decision_compute_ns);
    }
    std::sort(latency.begin(), latency.end());
    std::cout << "{\"schema\":\"polymarket_v7_native_crypto_decision_bench_v1\""
              << ",\"paper_only\":true,\"authenticated_execution\":false"
              << ",\"real_order_submission\":false,\"authority\":\"SHADOW_ZERO_AUTHORITY\""
              << ",\"scope\":\"PURE_CPP_SIGNAL_TO_EXECUTION_ADMISSION_KERNEL\""
              << ",\"samples\":" << samples
              << ",\"latency_ns\":{\"p50\":" << quantile(latency, .50)
              << ",\"p95\":" << quantile(latency, .95)
              << ",\"p99\":" << quantile(latency, .99)
              << ",\"p999\":" << quantile(latency, .999)
              << ",\"max\":" << latency.back() << "}}\n";
    return 0;
}
