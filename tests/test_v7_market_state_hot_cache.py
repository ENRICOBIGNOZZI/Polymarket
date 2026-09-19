from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROGRAM = r'''
#include "pm/v7_market_state.hpp"

#include <array>
#include <cassert>
#include <cstdint>
#include <vector>

using namespace pm::v7;

static DepthSummary depth_of(const std::vector<PriceLevelE4>& levels) {
    DepthSummary d{};
    std::int64_t cumulative = 0;
    for (std::size_t i = 0; i < levels.size() && i < kHotDepthLevels; ++i) {
        cumulative += levels[i].quantity_microunits;
        if (i == 0) d.l1_microunits = cumulative;
        if (i == 4) d.l5_microunits = cumulative;
        if (i == 9) d.l10_microunits = cumulative;
    }
    if (!levels.empty() && levels.size() < 5) d.l5_microunits = cumulative;
    if (!levels.empty() && levels.size() < 10) d.l10_microunits = cumulative;
    return d;
}

static void assert_levels_same(const std::array<PriceLevelE4, kHotDepthLevels>& lhs,
                               const std::array<PriceLevelE4, kHotDepthLevels>& rhs) {
    for (std::size_t i = 0; i < kHotDepthLevels; ++i) {
        assert(lhs[i].price_e4 == rhs[i].price_e4);
        assert(lhs[i].quantity_microunits == rhs[i].quantity_microunits);
    }
}

static void assert_cache_matches(const CanonicalL2Book& book,
                                 std::int64_t exchange_ns,
                                 std::int64_t receive_ns,
                                 bool lineage) {
    const auto s = book.hot_snapshot();
    assert(s.state_version == book.state_version());
    assert(s.exchange_event_ns == exchange_ns);
    assert(s.receive_monotonic_ns == receive_ns);
    assert(s.tick_size_e4 == book.tick_size_e4());
    assert(s.lineage_continuous == static_cast<std::uint8_t>(lineage));

    std::vector<PriceLevelE4> bids;
    std::vector<PriceLevelE4> asks;
    for (std::int32_t p = kCanonicalPriceScale - 1; p > 0; --p) {
        const auto q = book.quantity_at(Side::Buy, p);
        if (q > 0 && bids.size() < kHotDepthLevels) bids.push_back({p, q});
    }
    for (std::int32_t p = 1; p < kCanonicalPriceScale; ++p) {
        const auto q = book.quantity_at(Side::Sell, p);
        if (q > 0 && asks.size() < kHotDepthLevels) asks.push_back({p, q});
    }

    assert(s.bid_level_count == bids.size());
    assert(s.ask_level_count == asks.size());
    for (std::size_t i = 0; i < kHotDepthLevels; ++i) {
        const PriceLevelE4 eb = i < bids.size() ? bids[i] : PriceLevelE4{};
        const PriceLevelE4 ea = i < asks.size() ? asks[i] : PriceLevelE4{};
        assert(s.bid_levels[i].price_e4 == eb.price_e4);
        assert(s.bid_levels[i].quantity_microunits == eb.quantity_microunits);
        assert(s.ask_levels[i].price_e4 == ea.price_e4);
        assert(s.ask_levels[i].quantity_microunits == ea.quantity_microunits);
    }

    const auto bd = depth_of(bids);
    const auto ad = depth_of(asks);
    assert(s.bid_depth.l1_microunits == bd.l1_microunits);
    assert(s.bid_depth.l5_microunits == bd.l5_microunits);
    assert(s.bid_depth.l10_microunits == bd.l10_microunits);
    assert(s.ask_depth.l1_microunits == ad.l1_microunits);
    assert(s.ask_depth.l5_microunits == ad.l5_microunits);
    assert(s.ask_depth.l10_microunits == ad.l10_microunits);

    const auto best_bid = bids.empty() ? 0 : bids.front().price_e4;
    const auto best_ask = asks.empty() ? 0 : asks.front().price_e4;
    assert(s.best_bid_e4 == best_bid);
    assert(s.best_ask_e4 == best_ask);
    assert(s.best_bid_microunits == (bids.empty() ? 0 : bids.front().quantity_microunits));
    assert(s.best_ask_microunits == (asks.empty() ? 0 : asks.front().quantity_microunits));
    const bool valid = lineage && book.state_version() > 0 && exchange_ns > 0 && receive_ns > 0
        && !bids.empty() && !asks.empty() && best_ask > best_bid;
    assert(s.valid == static_cast<std::uint8_t>(valid));
}

int main() {
    CanonicalL2Book book(10);
    std::vector<PriceLevelE4> bids, asks;
    for (std::int32_t i = 0; i < 15; ++i) {
        bids.push_back({4900 - i * 10, 1'000'000 + i * 10'000});
        asks.push_back({5100 + i * 10, 2'000'000 + i * 10'000});
    }
    std::int64_t ex = 1'000'000, rx = 2'000'000;
    assert(book.replace_snapshot(bids, asks, ex, rx));
    assert_cache_matches(book, ex, rx, true);

    auto before = book.hot_snapshot();
    ++ex; ++rx;
    assert(book.mutate_level(Side::Buy, 4700, 3'000'000, ex, rx));
    auto after = book.hot_snapshot();
    assert_levels_same(before.bid_levels, after.bid_levels);
    assert(before.bid_depth.l10_microunits == after.bid_depth.l10_microunits);
    assert_cache_matches(book, ex, rx, true);

    ++ex; ++rx; assert(book.mutate_level(Side::Buy, 4900, 9'000'000, ex, rx));
    assert_cache_matches(book, ex, rx, true);
    ++ex; ++rx; assert(book.mutate_level(Side::Buy, 4860, 0, ex, rx));
    assert_cache_matches(book, ex, rx, true);
    ++ex; ++rx; assert(book.mutate_level(Side::Buy, 4950, 1'250'000, ex, rx));
    assert_cache_matches(book, ex, rx, true);

    before = book.hot_snapshot();
    ++ex; ++rx;
    assert(book.mutate_level(Side::Sell, 5300, 4'000'000, ex, rx));
    after = book.hot_snapshot();
    assert_levels_same(before.ask_levels, after.ask_levels);
    assert(before.ask_depth.l10_microunits == after.ask_depth.l10_microunits);
    assert_cache_matches(book, ex, rx, true);

    ++ex; ++rx; assert(book.mutate_level(Side::Sell, 5140, 0, ex, rx));
    assert_cache_matches(book, ex, rx, true);
    ++ex; ++rx; assert(book.mutate_level(Side::Sell, 5050, 1'500'000, ex, rx));
    assert_cache_matches(book, ex, rx, true);

    assert(book.set_tick_size(5));
    assert_cache_matches(book, ex, rx, true);
    assert(!book.set_tick_size(6));
    assert_cache_matches(book, ex, rx, true);
    ++ex; ++rx; assert(book.change_tick_size(5, ex, rx));
    assert_cache_matches(book, ex, rx, true);
    ++ex; ++rx; assert(book.mutate_level(Side::Buy, 4925, 777'000, ex, rx));
    assert_cache_matches(book, ex, rx, true);

    book.invalidate_lineage();
    assert_cache_matches(book, 0, 0, false);
    assert(!book.mutate_level(Side::Buy, 4920, 1'000'000, ex + 1, rx + 1));
    assert_cache_matches(book, 0, 0, false);
    return 0;
}
'''


def test_incremental_hot_snapshot_matches_full_book_state() -> None:
    compiler = shutil.which("c++")
    assert compiler, "C++ compiler required by V7 repository"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        main = path / "main.cpp"
        binary = path / "market-state-hot-cache-test"
        main.write_text(PROGRAM)
        subprocess.run([
            compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Wpedantic",
            f"-I{ROOT / 'include'}", str(ROOT / "src/v7_market_state.cpp"),
            str(main), "-o", str(binary),
        ], check=True, capture_output=True, text=True)
        subprocess.run([str(binary)], check=True, capture_output=True, text=True)
