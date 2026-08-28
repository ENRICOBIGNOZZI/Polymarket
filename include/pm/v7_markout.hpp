#pragma once

#include "pm/v7_market_state.hpp"

#include <cstdint>
#include <type_traits>

namespace pm::v7 {

// Fill-conditioned future liquidation measurement. `observable == 0` means
// the bounded L10 book cannot liquidate the full fill size and no markout must
// be fabricated from partial depth or a mid-price proxy.
struct ExecutableMarkout {
    double future_vwap = 0.0;
    double executable_liquidation_value = 0.0;
    double pnl_per_share = 0.0;
    std::uint8_t levels_used = 0;
    std::uint8_t observable = 0;
};

[[nodiscard]] ExecutableMarkout executable_markout(
    const BookHotSnapshot& future_book,
    Side fill_side,
    std::int64_t fill_quantity_microunits,
    std::int32_t fill_price_e4) noexcept;

static_assert(std::is_trivially_copyable_v<ExecutableMarkout>);

} // namespace pm::v7
