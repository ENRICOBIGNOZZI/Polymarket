#include "pm/v7_market_state.hpp"

#include <array>
#include <cassert>

namespace {

void test_snapshot_best_levels_and_cumulative_depth() {
    pm::v7::CanonicalL2Book book(100);
    const std::array<pm::v7::PriceLevelE4, 6> bids{{
        {4800, 1'000'000}, {4700, 2'000'000}, {4600, 3'000'000},
        {4500, 4'000'000}, {4400, 5'000'000}, {4300, 6'000'000},
    }};
    const std::array<pm::v7::PriceLevelE4, 6> asks{{
        {5200, 1'500'000}, {5300, 2'500'000}, {5400, 3'500'000},
        {5500, 4'500'000}, {5600, 5'500'000}, {5700, 6'500'000},
    }};
    assert(book.replace_snapshot(bids, asks, 100, 1000));
    const auto hot = book.hot_snapshot();
    assert(hot.valid);
    assert(hot.best_bid_e4 == 4800);
    assert(hot.best_ask_e4 == 5200);
    assert(book.venue_tick_index(hot.best_bid_e4) == 48);
    assert(book.venue_tick_index(hot.best_ask_e4) == 52);
    assert(hot.bid_depth.l1_microunits == 1'000'000);
    assert(hot.bid_depth.l5_microunits == 15'000'000);
    assert(hot.bid_depth.l10_microunits == 21'000'000);
    assert(hot.ask_depth.l5_microunits == 17'500'000);
}

void test_delete_best_uses_occupancy_index_without_sorting() {
    pm::v7::CanonicalL2Book book(100);
    const std::array<pm::v7::PriceLevelE4, 2> bids{{{4800, 2'000'000}, {4700, 3'000'000}}};
    const std::array<pm::v7::PriceLevelE4, 2> asks{{{5200, 2'000'000}, {5300, 3'000'000}}};
    assert(book.replace_snapshot(bids, asks, 100, 1000));
    assert(book.mutate_level(pm::v7::Side::Buy, 4800, 0, 101, 1001));
    assert(book.hot_snapshot().best_bid_e4 == 4700);
    assert(book.mutate_level(pm::v7::Side::Sell, 5200, 0, 102, 1002));
    assert(book.hot_snapshot().best_ask_e4 == 5300);
}

void test_crossed_or_off_tick_updates_fail_closed() {
    pm::v7::CanonicalL2Book book(100);
    const std::array<pm::v7::PriceLevelE4, 1> bids{{{4800, 1'000'000}}};
    const std::array<pm::v7::PriceLevelE4, 1> asks{{{5200, 1'000'000}}};
    assert(book.replace_snapshot(bids, asks, 100, 1000));
    const auto version = book.state_version();
    assert(!book.mutate_level(pm::v7::Side::Buy, 5250, 1'000'000, 101, 1001));
    assert(!book.mutate_level(pm::v7::Side::Buy, 5200, 1'000'000, 101, 1001));
    assert(book.state_version() == version);
    assert(book.hot_snapshot().best_bid_e4 == 4800);
}

void test_tick_change_requires_existing_levels_to_remain_valid() {
    pm::v7::CanonicalL2Book book(10);
    const std::array<pm::v7::PriceLevelE4, 1> bids{{{4850, 1'000'000}}};
    const std::array<pm::v7::PriceLevelE4, 1> asks{{{5150, 1'000'000}}};
    assert(book.replace_snapshot(bids, asks, 100, 1000));
    assert(!book.set_tick_size(100));
    assert(book.tick_size_e4() == 10);
    assert(book.set_tick_size(50));
    assert(book.tick_size_e4() == 50);
}

void test_receive_clock_cannot_go_backwards() {
    pm::v7::CanonicalL2Book book(100);
    const std::array<pm::v7::PriceLevelE4, 1> bids{{{4800, 1'000'000}}};
    const std::array<pm::v7::PriceLevelE4, 1> asks{{{5200, 1'000'000}}};
    assert(book.replace_snapshot(bids, asks, 100, 1000));
    assert(!book.mutate_level(pm::v7::Side::Buy, 4700, 1'000'000, 101, 999));
    assert(book.quantity_at(pm::v7::Side::Buy, 4700) == 0);
}

} // namespace

int main() {
    test_snapshot_best_levels_and_cumulative_depth();
    test_delete_best_uses_occupancy_index_without_sorting();
    test_crossed_or_off_tick_updates_fail_closed();
    test_tick_change_requires_existing_levels_to_remain_valid();
    test_receive_clock_cannot_go_backwards();
    return 0;
}
