#include "pm/v7_coinbase_l2.hpp"

#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <new>

namespace { std::atomic<std::uint64_t> allocations{0}; }
void* operator new(std::size_t size) {
    allocations.fetch_add(1, std::memory_order_relaxed);
    if (void* value=std::malloc(size)) return value;
    throw std::bad_alloc();
}
void operator delete(void* value) noexcept { std::free(value); }
void operator delete(void* value, std::size_t) noexcept { std::free(value); }

using namespace pm::v7::external_fair;

int main() {
    CoinbaseL2Book book;
    assert(!book.apply_update({11, {{true, 100.0, 2.0}}}));
    assert(book.state() == CoinbaseL2State::Gapped);
    book.begin_recovery();
    const std::array<CoinbaseDepthLevel,2> bids{{{100.0,1.0},{99.0,2.0}}};
    const std::array<CoinbaseDepthLevel,2> asks{{{101.0,3.0},{102.0,4.0}}};
    assert(book.install_snapshot(10,bids,asks));
    const std::array<CoinbaseDepthChange,3> changes{{{true,100.0,0.0},{true,100.5,5.0},{false,101.0,2.0}}};
    const auto before=allocations.load(std::memory_order_relaxed);
    assert(book.apply_update(11,changes));
    const auto after=allocations.load(std::memory_order_relaxed);
    assert(after==before);
    const auto updated = book.metrics();
    assert(updated.valid == 1 && updated.update_count == 1);
    assert(std::abs(updated.best_bid - 100.5) < 1e-12);
    assert(std::abs(updated.best_ask - 101.0) < 1e-12);
    assert(std::abs(updated.bid_depth_l1 - 5.0) < 1e-12);
    assert(std::abs(updated.ask_depth_l1 - 2.0) < 1e-12);

    assert(!book.apply_update({12, {{false, 100.0, 1.0}}}));
    assert(book.state() == CoinbaseL2State::Gapped && book.metrics().valid == 0);
    book.begin_recovery();
    assert(!book.install_snapshot({20, {{200.0, 1.0}}, {{200.0, 1.0}}}));
    assert(book.state() == CoinbaseL2State::Gapped);
    std::cout << "v7 Coinbase L2 tests passed\n";
}
