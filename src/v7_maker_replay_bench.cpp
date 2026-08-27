#include "pm/v7_maker_hft.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <vector>

namespace {

std::int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

std::int64_t quantile(std::vector<std::int64_t> values, double p) {
    if (values.empty()) return 0;
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>(std::llround(
        std::clamp(p, 0.0, 1.0) * static_cast<double>(values.size() - 1)));
    return values[index];
}

} // namespace

int main(int argc, char** argv) {
    std::size_t events = 200'000;
    if (argc > 1) events = std::max<std::size_t>(1, std::strtoull(argv[1], nullptr, 10));

    pm::v7::maker::MakerModelSnapshot model;
    model.tick_size = 0.01;
    model.base_ev_se_per_share = 0.0001;
    model.base_quote_shares = 2.0;

    pm::v7::maker::MakerHotPath hot;
    pm::v7::maker::InventorySnapshot inventory;
    pm::v7::maker::QuoteSnapshot quotes;
    pm::v7::maker::RiskSnapshot risk;
    risk.max_quote_shares = 2.0;
    risk.max_abs_residual_shares = 50.0;
    risk.max_local_state_age_ns = 5'000'000'000LL;

    pm::v7::maker::MarketUpdate update;
    update.market_handle = 1;
    update.event_handle = 2;
    update.instrument_handle = 3;
    update.best_bid_tick = 48;
    update.best_ask_tick = 52;
    update.bid_depth_l1 = 25.0;
    update.ask_depth_l1 = 25.0;
    update.bid_depth_l5 = 100.0;
    update.ask_depth_l5 = 100.0;
    update.bid_depth_l10 = 180.0;
    update.ask_depth_l10 = 180.0;
    update.feed_healthy = 1;

    std::vector<std::int64_t> receive_to_intent;
    std::vector<std::int64_t> kernel_ns;
    receive_to_intent.reserve(events);
    kernel_ns.reserve(events);

    std::size_t intents = 0;
    for (std::size_t i = 0; i < events; ++i) {
        update.state_version = i + 1;
        const double wave = static_cast<double>(static_cast<int>(i % 41) - 20);
        update.bid_depth_l5 = 100.0 + wave * 0.25;
        update.ask_depth_l5 = 100.0 - wave * 0.20;
        update.trade_buy_qty_delta = (i % 7 == 0) ? 2.0 : 0.0;
        update.trade_sell_qty_delta = (i % 11 == 0) ? 1.5 : 0.0;
        update.cancel_bid_qty_delta = (i % 13 == 0) ? 3.0 : 0.0;
        update.cancel_ask_qty_delta = (i % 17 == 0) ? 2.5 : 0.0;
        update.socket_receive_monotonic_ns = now_ns();

        const auto begin = now_ns();
        const auto decision = hot.on_market_update(update, inventory, quotes, risk, model);
        const auto end = now_ns();
        kernel_ns.push_back(end - begin);
        receive_to_intent.push_back(decision.latency.receive_to_intent_ns);
        intents += decision.intent_count;
    }

    const auto p50 = quantile(receive_to_intent, 0.50);
    const auto p90 = quantile(receive_to_intent, 0.90);
    const auto p95 = quantile(receive_to_intent, 0.95);
    const auto p99 = quantile(receive_to_intent, 0.99);
    const auto p999 = quantile(receive_to_intent, 0.999);
    const auto maximum = *std::max_element(receive_to_intent.begin(), receive_to_intent.end());
    const auto kernel_p99 = quantile(kernel_ns, 0.99);

    std::cout
        << "{\"events\":" << events
        << ",\"intents\":" << intents
        << ",\"receive_to_intent_ns\":{\"p50\":" << p50
        << ",\"p90\":" << p90
        << ",\"p95\":" << p95
        << ",\"p99\":" << p99
        << ",\"p99_9\":" << p999
        << ",\"max\":" << maximum
        << "},\"kernel_p99_ns\":" << kernel_p99
        << ",\"representative_venue_replay\":false"
        << ",\"paper_only\":true"
        << "}\n";
    return 0;
}
