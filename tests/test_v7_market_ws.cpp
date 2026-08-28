#include "pm/v7_market_ws.hpp"

#include <array>
#include <cassert>
#include <string>
#include <string_view>
#include <vector>

namespace {

pm::v7::MarketWsShard make_shard() {
    std::vector<pm::v7::TokenBinding> bindings;
    bindings.push_back(pm::v7::TokenBinding{
        .asset_id = "asset-yes",
        .market_handle = 101,
        .event_handle = 201,
        .instrument_handle = 301,
        .tick_size_e4 = 100,
    });
    bindings.push_back(pm::v7::TokenBinding{
        .asset_id = "asset-no",
        .market_handle = 101,
        .event_handle = 201,
        .instrument_handle = 302,
        .tick_size_e4 = 100,
    });
    return pm::v7::MarketWsShard(std::move(bindings));
}

pm::fast::FeedReceiveStamp stamp(std::int64_t monotonic_ns, std::int64_t wall_ms) {
    pm::fast::FeedReceiveStamp out;
    out.wall_ms = wall_ms;
    out.monotonic_ns = monotonic_ns;
    return out;
}

void test_snapshot_delta_trade_and_reconnect_lineage() {
    auto shard = make_shard();
    std::array<pm::v7::MarketWsEvent, 16> output{};

    constexpr std::string_view snapshot = R"JSON({
      "event_type":"book",
      "asset_id":"asset-yes",
      "timestamp":1700000000000,
      "bids":[{"price":"0.48","size":"2.0"},{"price":"0.47","size":"3.0"}],
      "asks":[{"price":"0.52","size":"4.0"},{"price":"0.53","size":"5.0"}]
    })JSON";
    auto result = shard.process_frame(snapshot, stamp(2'000'000'000LL, 1'700'000'000'000LL), output);
    assert(!result.invalid_frame);
    assert(result.output_count == 1);
    assert(result.applied_state_events == 1);
    assert(output[0].kind == pm::v7::MarketWsEventKind::BookChanged);
    assert(output[0].instrument_handle == 301);
    assert(output[0].receive_monotonic_ns == 2'000'000'000LL);
    assert(output[0].book.receive_monotonic_ns == 2'000'000'000LL);
    assert(output[0].book.valid);
    assert(output[0].book.best_bid_e4 == 4800);
    assert(output[0].book.best_ask_e4 == 5200);
    assert(output[0].book.best_bid_microunits == 2'000'000);
    assert(output[0].book.best_ask_microunits == 4'000'000);

    constexpr std::string_view delta = R"JSON({
      "event_type":"price_change",
      "timestamp":1700000000100,
      "price_changes":[{
        "asset_id":"asset-yes",
        "side":"BUY",
        "price":"0.48",
        "size":"1.5"
      }]
    })JSON";
    result = shard.process_frame(delta, stamp(2'100'000'000LL, 1'700'000'000'100LL), output);
    assert(!result.invalid_frame);
    assert(result.applied_state_events == 1);
    assert(result.output_count == 1);
    assert(output[0].price_e4 == 4800);
    assert(output[0].quantity_microunits == 1'500'000);
    assert(output[0].book.best_bid_microunits == 1'500'000);

    constexpr std::string_view trade = R"JSON({
      "event_type":"last_trade_price",
      "asset_id":"asset-yes",
      "timestamp":1700000000200,
      "side":"BUY",
      "price":"0.52",
      "size":"0.7"
    })JSON";
    result = shard.process_frame(trade, stamp(2'200'000'000LL, 1'700'000'000'200LL), output);
    assert(!result.invalid_frame);
    assert(result.trade_events == 1);
    assert(result.output_count == 1);
    assert(output[0].kind == pm::v7::MarketWsEventKind::Trade);
    assert(output[0].side == pm::v7::Side::Buy);
    assert(output[0].price_e4 == 5200);
    assert(output[0].quantity_microunits == 700'000);

    shard.invalidate_all_lineage();
    assert(!shard.snapshot(301).valid);
    assert(!shard.snapshot(302).valid);

    result = shard.process_frame(delta, stamp(2'300'000'000LL, 1'700'000'000'300LL), output);
    assert(result.lineage_invalidated);
    assert(result.output_count == 1);
    assert(output[0].kind == pm::v7::MarketWsEventKind::LineageInvalidated);
    assert(!shard.snapshot(301).valid);

    result = shard.process_frame(snapshot, stamp(2'400'000'000LL, 1'700'000'000'400LL), output);
    assert(!result.invalid_frame);
    assert(result.applied_state_events == 1);
    assert(shard.snapshot(301).valid);
}

void test_large_live_snapshot_frame_fits_bounded_arena() {
    auto shard = make_shard();
    std::array<pm::v7::MarketWsEvent, 8> output{};

    std::string snapshot = R"JSON({"event_type":"book","asset_id":"asset-yes","timestamp":1700000000300,"bids":[{"price":"0.48","size":"2"}],"asks":[{"price":"0.52","size":"3"}],"venue_payload_padding":")JSON";
    snapshot.append(2 * 1024 * 1024, 'x');
    snapshot += "\"}";

    const auto result = shard.process_frame(
        snapshot, stamp(2'500'000'000LL, 1'700'000'000'300LL), output);
    assert(!result.invalid_frame);
    assert(!result.arena_exhausted);
    assert(!result.output_overflow);
    assert(result.applied_state_events == 1);
    assert(result.output_count == 1);
    assert(shard.snapshot(301).valid);
}

void test_unknown_asset_is_ignored_without_poisoning_known_lineage() {
    auto shard = make_shard();
    std::array<pm::v7::MarketWsEvent, 8> output{};

    constexpr std::string_view snapshot = R"JSON({
      "event_type":"book",
      "asset_id":"asset-yes",
      "timestamp":1700000000000,
      "bids":[{"price":"0.48","size":"2"}],
      "asks":[{"price":"0.52","size":"3"}]
    })JSON";
    auto result = shard.process_frame(snapshot, stamp(3'000'000'000LL, 1'700'000'001'000LL), output);
    assert(!result.invalid_frame);
    assert(shard.snapshot(301).valid);

    constexpr std::string_view unknown = R"JSON({
      "event_type":"book",
      "asset_id":"not-bound",
      "timestamp":1700000000100,
      "bids":[{"price":"0.40","size":"1"}],
      "asks":[{"price":"0.60","size":"1"}]
    })JSON";
    result = shard.process_frame(unknown, stamp(3'100'000'000LL, 1'700'000'001'100LL), output);
    assert(!result.invalid_frame);
    assert(result.ignored_unknown_assets == 1);
    assert(result.output_count == 0);
    assert(shard.snapshot(301).valid);
}

void test_malformed_or_unconsumable_frame_invalidates_continuity() {
    auto shard = make_shard();
    std::array<pm::v7::MarketWsEvent, 8> output{};

    constexpr std::string_view snapshot = R"JSON({
      "event_type":"book",
      "asset_id":"asset-yes",
      "timestamp":1700000000000,
      "bids":[{"price":"0.48","size":"2"}],
      "asks":[{"price":"0.52","size":"3"}]
    })JSON";
    auto result = shard.process_frame(snapshot, stamp(4'000'000'000LL, 1'700'000'002'000LL), output);
    assert(!result.invalid_frame);
    assert(shard.snapshot(301).valid);

    result = shard.process_frame("{bad-json", stamp(4'100'000'000LL, 1'700'000'002'100LL), output);
    assert(result.invalid_frame);
    assert(result.lineage_invalidated);
    assert(!shard.snapshot(301).valid);

    result = shard.process_frame(snapshot, stamp(4'200'000'000LL, 1'700'000'002'200LL), output);
    assert(shard.snapshot(301).valid);

    std::span<pm::v7::MarketWsEvent> no_output;
    result = shard.process_frame(snapshot, stamp(4'300'000'000LL, 1'700'000'002'300LL), no_output);
    assert(result.output_overflow);
    assert(result.lineage_invalidated);
    assert(!shard.snapshot(301).valid);
}

} // namespace

int main() {
    test_snapshot_delta_trade_and_reconnect_lineage();
    test_large_live_snapshot_frame_fits_bounded_arena();
    test_unknown_asset_is_ignored_without_poisoning_known_lineage();
    test_malformed_or_unconsumable_frame_invalidates_continuity();
    return 0;
}
