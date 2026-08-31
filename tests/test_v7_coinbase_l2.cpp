#include "pm/v7_coinbase_l2.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

using namespace pm::v7::external_fair;

int main() {
    CoinbaseL2Book book;
    assert(!book.apply_update({11, {{true, 100.0, 2.0}}}));
    assert(book.state() == CoinbaseL2State::Gapped);
    book.begin_recovery();
    assert(book.install_snapshot({10, {{100.0, 1.0}, {99.0, 2.0}}, {{101.0, 3.0}, {102.0, 4.0}}}));
    assert(book.apply_update({11, {{true, 100.0, 0.0}, {true, 100.5, 5.0}, {false, 101.0, 2.0}}}));
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
