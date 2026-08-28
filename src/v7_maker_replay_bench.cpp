#include "pm/v7_maker_lane.hpp"
#include "pm/v7_market_ws.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string_view>
#include <utility>
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

    // Warm caches and branch predictors. Samples used by the gate begin only
    // after this phase; this is an internal compute guardrail, not venue proof.
    const std::size_t warmup = std::min<std::size_t>(20'000, std::max<std::size_t>(1, events / 10));
    for (std::size_t i = 0; i < warmup; ++i) {
        update.state_version = i + 1;
        update.socket_receive_monotonic_ns = now_ns();
        (void)hot.on_market_update(update, inventory, quotes, risk, model);
    }

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

    const auto hot_p50 = quantile(receive_to_intent, 0.50);
    const auto hot_p90 = quantile(receive_to_intent, 0.90);
    const auto hot_p95 = quantile(receive_to_intent, 0.95);
    const auto hot_p99 = quantile(receive_to_intent, 0.99);
    const auto hot_p999 = quantile(receive_to_intent, 0.999);
    const auto hot_maximum = *std::max_element(receive_to_intent.begin(), receive_to_intent.end());

    std::vector<pm::v7::TokenBinding> bindings;
    bindings.push_back(pm::v7::TokenBinding{
        .asset_id = "latency-asset",
        .market_handle = 1,
        .event_handle = 2,
        .instrument_handle = 3,
        .tick_size_e4 = 100,
    });
    pm::v7::MarketWsShard decoder(std::move(bindings));
    pm::v7::maker::MakerInstrumentLane lane(+1);
    pm::v7::maker::MakerLaneContext lane_context;
    lane_context.risk = risk;
    constexpr std::string_view payload = R"JSON({
      "event_type":"book",
      "asset_id":"latency-asset",
      "timestamp":1700000000000,
      "bids":[{"price":"0.48","size":"25"},{"price":"0.47","size":"75"}],
      "asks":[{"price":"0.52","size":"25"},{"price":"0.53","size":"75"}]
    })JSON";
    std::array<pm::v7::MarketWsEvent, 4> output{};
    std::vector<std::int64_t> parse_ns;
    std::vector<std::int64_t> book_ns;
    std::vector<std::int64_t> feature_ns;
    std::vector<std::int64_t> decision_ns;
    std::vector<std::int64_t> risk_ns;
    std::vector<std::int64_t> pipeline_ns;
    const std::size_t pipeline_events = std::min<std::size_t>(events, 100'000);
    for (auto* samples : {&parse_ns, &book_ns, &feature_ns, &decision_ns, &risk_ns, &pipeline_ns}) {
        samples->reserve(pipeline_events);
    }
    for (std::size_t i = 0; i < pipeline_events + warmup; ++i) {
        pm::fast::FeedReceiveStamp stamp;
        stamp.wall_ms = 1'700'000'000'000LL;
        stamp.monotonic_ns = now_ns();
        const auto frame = decoder.process_frame(payload, stamp, output);
        if (frame.invalid_frame || frame.output_count != 1) return 2;
        const auto pipeline_decision = lane.on_market_event(output[0], lane_context, model);
        if (i < warmup) continue;
        parse_ns.push_back(frame.parse_ns);
        book_ns.push_back(frame.book_apply_ns);
        feature_ns.push_back(pipeline_decision.latency.feature_ns);
        decision_ns.push_back(pipeline_decision.latency.decision_ns);
        risk_ns.push_back(pipeline_decision.latency.risk_ns);
        pipeline_ns.push_back(pipeline_decision.latency.receive_to_intent_ns);
    }

    auto series = [](const std::vector<std::int64_t>& values) {
        return std::array<std::int64_t, 6>{
            quantile(values, 0.50), quantile(values, 0.90), quantile(values, 0.95),
            quantile(values, 0.99), quantile(values, 0.999),
            *std::max_element(values.begin(), values.end())};
    };
    const auto pipeline = series(pipeline_ns);
    const auto parse = series(parse_ns);
    const auto book = series(book_ns);
    const auto feature = series(feature_ns);
    const auto decision_stage = series(decision_ns);
    const auto risk_stage = series(risk_ns);
    auto emit_series = [](const char* name, const std::array<std::int64_t, 6>& x) {
        std::cout << "\"" << name << "\":{\"p50\":" << x[0]
                  << ",\"p90\":" << x[1] << ",\"p95\":" << x[2]
                  << ",\"p99\":" << x[3] << ",\"p99_9\":" << x[4]
                  << ",\"max\":" << x[5] << "}";
    };

    std::cout
        << "{\"events\":" << events
        << ",\"pipeline_events\":" << pipeline_events
        << ",\"intents\":" << intents
        << ",\"hot_receive_to_intent_ns\":{\"p50\":" << hot_p50
        << ",\"p90\":" << hot_p90 << ",\"p95\":" << hot_p95
        << ",\"p99\":" << hot_p99 << ",\"p99_9\":" << hot_p999
        << ",\"max\":" << hot_maximum << "},\"stages_ns\":{";
    emit_series("parse", parse);
    std::cout << ',';
    emit_series("book", book);
    std::cout << ',';
    emit_series("features", feature);
    std::cout << ',';
    emit_series("decision", decision_stage);
    std::cout << ',';
    emit_series("risk", risk_stage);
    std::cout << ',';
    emit_series("receive_to_intent", pipeline);
    std::cout << "},\"representative_venue_replay\":false"
              << ",\"includes_network_or_clob\":false"
              << ",\"paper_only\":true}\n";
    return 0;
}
