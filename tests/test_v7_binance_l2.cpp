#include "pm/v7_binance_l2.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

using namespace pm::v7::external_fair;

namespace {

BinanceDepthDelta delta(std::uint64_t first, std::uint64_t last, std::int64_t receive,
                        std::initializer_list<BinanceDepthLevel> bids,
                        std::initializer_list<BinanceDepthLevel> asks) {
    BinanceDepthDelta out;
    out.first_update_id = first;
    out.final_update_id = last;
    out.local_receive_monotonic_ns = receive;
    out.bids = bids;
    out.asks = asks;
    return out;
}

BinanceDepthSnapshot snapshot(std::uint64_t id, std::int64_t receive,
                              std::initializer_list<BinanceDepthLevel> bids,
                              std::initializer_list<BinanceDepthLevel> asks) {
    BinanceDepthSnapshot out;
    out.last_update_id = id;
    out.local_receive_monotonic_ns = receive;
    out.bids = bids;
    out.asks = asks;
    return out;
}

} // namespace

int main() {
    BinanceL2Book book;
    assert(book.state() == BinanceL2State::Buffering);
    assert(book.buffer_delta(delta(101, 102, 11, {{100.0, 3.0}, {99.0, 2.0}}, {{101.0, 4.0}})));
    assert(book.buffer_delta(delta(103, 103, 12, {{100.5, 5.0}}, {{101.0, 0.0}, {101.5, 6.0}})));
    assert(book.install_snapshot(snapshot(100, 10, {{100.0, 1.0}, {99.0, 2.0}}, {{101.0, 1.0}, {102.0, 3.0}})));
    const auto initial = book.metrics();
    assert(initial.valid == 1 && book.state() == BinanceL2State::Live);
    assert(initial.last_update_id == 103);
    assert(std::abs(initial.best_bid - 100.5) < 1e-12);
    assert(std::abs(initial.best_ask - 101.5) < 1e-12);
    assert(std::abs(initial.bid_depth_l1 - 5.0) < 1e-12);
    assert(std::abs(initial.ask_depth_l5 - 9.0) < 1e-12);
    assert(initial.spread_bps > 0.0 && initial.imbalance_l1 < 0.0);

    assert(book.apply_live_delta(delta(104, 104, 13, {{100.5, 0.0}, {100.25, 8.0}}, {{101.5, 2.0}})));
    const auto changed = book.metrics();
    assert(changed.last_update_id == 104 && std::abs(changed.best_bid - 100.25) < 1e-12);
    assert(std::abs(changed.ask_depth_l1 - 2.0) < 1e-12);

    assert(!book.apply_live_delta(delta(106, 106, 14, {{100.25, 1.0}}, {{101.5, 2.0}})));
    assert(book.state() == BinanceL2State::Gapped && book.metrics().valid == 0);
    book.begin_recovery();
    assert(book.buffer_delta(delta(205, 205, 21, {{200.0, 2.0}}, {{201.0, 3.0}})));
    assert(book.install_snapshot(snapshot(204, 20, {{200.0, 1.0}}, {{201.0, 1.0}})));
    assert(book.metrics().valid == 1 && book.metrics().last_update_id == 205);

    BinanceL2Book overflow(1);
    assert(overflow.buffer_delta(delta(1, 1, 1, {{10.0, 1.0}}, {{11.0, 1.0}})));
    assert(!overflow.buffer_delta(delta(2, 2, 2, {{10.0, 1.0}}, {{11.0, 1.0}})));
    assert(overflow.state() == BinanceL2State::Gapped);
    std::cout << "v7 Binance L2 tests passed\n";
}
