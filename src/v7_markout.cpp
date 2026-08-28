#include "pm/v7_markout.hpp"

#include <algorithm>
#include <array>
#include <cstddef>

namespace pm::v7 {

ExecutableMarkout executable_markout(
    const BookHotSnapshot& future_book,
    Side fill_side,
    std::int64_t fill_quantity_microunits,
    std::int32_t fill_price_e4) noexcept {

    ExecutableMarkout out;
    if (future_book.valid == 0 || future_book.lineage_continuous == 0
        || fill_side == Side::None || fill_quantity_microunits <= 0
        || fill_price_e4 <= 0 || fill_price_e4 >= kCanonicalPriceScale) {
        return out;
    }

    const auto& ladder = fill_side == Side::Buy ? future_book.bid_levels : future_book.ask_levels;
    const std::size_t level_count = std::min<std::size_t>(
        fill_side == Side::Buy ? future_book.bid_level_count : future_book.ask_level_count,
        kHotDepthLevels);
    if (level_count == 0) return out;

    std::int64_t remaining = fill_quantity_microunits;
    long double notional_e4_microunits = 0.0L;
    std::uint8_t levels_used = 0;
    for (std::size_t i = 0; i < level_count && remaining > 0; ++i) {
        const auto& level = ladder[i];
        if (level.price_e4 <= 0 || level.price_e4 >= kCanonicalPriceScale
            || level.quantity_microunits <= 0) {
            continue;
        }
        const std::int64_t take = std::min(remaining, level.quantity_microunits);
        if (take <= 0) continue;
        notional_e4_microunits += static_cast<long double>(take)
                                * static_cast<long double>(level.price_e4);
        remaining -= take;
        ++levels_used;
    }
    if (remaining > 0 || levels_used == 0) return out;

    const long double vwap_e4 = notional_e4_microunits
                              / static_cast<long double>(fill_quantity_microunits);
    const double future_vwap = static_cast<double>(vwap_e4 / 10'000.0L);
    const double fill_price = static_cast<double>(fill_price_e4) / 10'000.0;
    const double shares = static_cast<double>(fill_quantity_microunits) / 1'000'000.0;

    out.future_vwap = future_vwap;
    out.executable_liquidation_value = shares * future_vwap;
    out.pnl_per_share = fill_side == Side::Buy
        ? future_vwap - fill_price
        : fill_price - future_vwap;
    out.levels_used = levels_used;
    out.observable = 1;
    return out;
}

} // namespace pm::v7
