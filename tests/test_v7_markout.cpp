#include "pm/v7_markout.hpp"

#include <cassert>
#include <cmath>

namespace {

bool close(double a, double b, double tol = 1e-12) {
    return std::abs(a - b) <= tol;
}

pm::v7::BookHotSnapshot future_book() {
    pm::v7::BookHotSnapshot book;
    book.valid = 1;
    book.lineage_continuous = 1;
    book.state_version = 7;
    book.exchange_event_ns = 2'000'000'000LL;
    book.receive_monotonic_ns = 3'000'000'000LL;
    book.tick_size_e4 = 100;
    book.best_bid_e4 = 5000;
    book.best_ask_e4 = 5200;
    book.bid_levels[0] = {5000, 2'000'000};
    book.bid_levels[1] = {4900, 3'000'000};
    book.bid_level_count = 2;
    book.ask_levels[0] = {5200, 1'000'000};
    book.ask_levels[1] = {5300, 4'000'000};
    book.ask_level_count = 2;
    return book;
}

void test_buy_markout_walks_future_bids() {
    const auto result = pm::v7::executable_markout(
        future_book(), pm::v7::Side::Buy, 4'000'000, 5100);
    assert(result.observable);
    assert(result.levels_used == 2);
    assert(close(result.future_vwap, 0.495));
    assert(close(result.executable_liquidation_value, 1.98));
    assert(close(result.pnl_per_share, -0.015));
}

void test_sell_markout_walks_future_asks() {
    const auto result = pm::v7::executable_markout(
        future_book(), pm::v7::Side::Sell, 3'000'000, 5200);
    assert(result.observable);
    assert(result.levels_used == 2);
    assert(close(result.future_vwap, 1.58 / 3.0));
    assert(close(result.executable_liquidation_value, 1.58));
    assert(close(result.pnl_per_share, 0.52 - 1.58 / 3.0));
}

void test_insufficient_depth_is_unobservable_not_partial() {
    const auto result = pm::v7::executable_markout(
        future_book(), pm::v7::Side::Buy, 6'000'000, 5100);
    assert(!result.observable);
    assert(result.levels_used == 0);
    assert(result.executable_liquidation_value == 0.0);
    assert(result.pnl_per_share == 0.0);
}

void test_invalid_or_lineage_broken_book_is_unobservable() {
    auto book = future_book();
    book.lineage_continuous = 0;
    assert(!pm::v7::executable_markout(book, pm::v7::Side::Buy, 1'000'000, 5100).observable);
    book = future_book();
    book.valid = 0;
    assert(!pm::v7::executable_markout(book, pm::v7::Side::Sell, 1'000'000, 5200).observable);
}

} // namespace

int main() {
    test_buy_markout_walks_future_bids();
    test_sell_markout_walks_future_asks();
    test_insufficient_depth_is_unobservable_not_partial();
    test_invalid_or_lineage_broken_book_is_unobservable();
    return 0;
}
