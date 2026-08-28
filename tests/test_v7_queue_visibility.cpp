#include "pm/v7_queue_visibility.hpp"

#include <cassert>

namespace {

pm::v7::StrategyIntent quote(pm::v7::Side side, std::int64_t tick) {
    pm::v7::StrategyIntent intent;
    intent.intent_id = 1;
    intent.type = pm::v7::IntentType::Quote;
    intent.side = side;
    intent.price_tick = tick;
    intent.quantity_microunits = 1'000'000;
    return intent;
}

pm::v7::BookHotSnapshot book() {
    pm::v7::BookHotSnapshot b;
    b.tick_size_e4 = 100;
    b.best_bid_e4 = 4800;
    b.best_ask_e4 = 5200;
    b.valid = 1;
    b.lineage_continuous = 1;
    b.bid_level_count = 3;
    b.bid_levels[0] = {4800, 20'000'000};
    b.bid_levels[1] = {4700, 30'000'000};
    b.bid_levels[2] = {4500, 40'000'000}; // deliberate 0-depth hole at 0.46
    b.ask_level_count = 3;
    b.ask_levels[0] = {5200, 25'000'000};
    b.ask_levels[1] = {5300, 35'000'000};
    b.ask_levels[2] = {5500, 45'000'000}; // deliberate hole at 0.54
    return b;
}

void test_exact_resting_level_not_whole_l10() {
    const auto b = book();
    const auto bid = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Buy, 47), b);
    assert(bid.observable);
    assert(bid.visible_ahead_microunits == 30'000'000);
    const auto ask = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Sell, 53), b);
    assert(ask.observable);
    assert(ask.visible_ahead_microunits == 35'000'000);
}

void test_visible_hole_is_observable_zero() {
    const auto b = book();
    const auto bid = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Buy, 46), b);
    assert(bid.observable);
    assert(bid.visible_ahead_microunits == 0);
    const auto ask = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Sell, 54), b);
    assert(ask.observable);
    assert(ask.visible_ahead_microunits == 0);
}

void test_price_improvement_has_zero_queue_ahead() {
    const auto b = book();
    const auto bid = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Buy, 49), b);
    assert(bid.observable && bid.visible_ahead_microunits == 0);
    const auto ask = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Sell, 51), b);
    assert(ask.observable && ask.visible_ahead_microunits == 0);
}

void test_short_ladder_proves_no_deeper_public_level() {
    const auto b = book();
    const auto deep_bid = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Buy, 40), b);
    assert(deep_bid.observable);
    assert(deep_bid.visible_ahead_microunits == 0);
    const auto deep_ask = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Sell, 60), b);
    assert(deep_ask.observable);
    assert(deep_ask.visible_ahead_microunits == 0);
}

void test_full_l10_deeper_quote_is_unobservable() {
    auto b = book();
    b.bid_level_count = 10;
    b.ask_level_count = 10;
    for (std::size_t i = 0; i < 10; ++i) {
        b.bid_levels[i] = {static_cast<std::int32_t>(4800 - 100 * static_cast<int>(i)), 1'000'000};
        b.ask_levels[i] = {static_cast<std::int32_t>(5200 + 100 * static_cast<int>(i)), 1'000'000};
    }
    const auto bid = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Buy, 38), b);
    assert(!bid.observable);
    const auto ask = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Sell, 62), b);
    assert(!ask.observable);
}

void test_invalid_lineage_is_unobservable() {
    auto b = book();
    b.lineage_continuous = 0;
    const auto q = pm::v7::visible_queue_at_quote(quote(pm::v7::Side::Buy, 48), b);
    assert(!q.observable);
}

} // namespace

int main() {
    test_exact_resting_level_not_whole_l10();
    test_visible_hole_is_observable_zero();
    test_price_improvement_has_zero_queue_ahead();
    test_short_ladder_proves_no_deeper_public_level();
    test_full_l10_deeper_quote_is_unobservable();
    test_invalid_lineage_is_unobservable();
    return 0;
}
